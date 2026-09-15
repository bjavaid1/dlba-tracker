import os
import json
import subprocess
from html import escape

import requests


# --- Configuration ---
API_URL = (
    "https://services2.arcgis.com/qvkbeam7Wirps6zC/"
    "arcgis/rest/services/DLBA_Owned_Properties/FeatureServer/0/query"
)

STATE_FILE = "data_state.json"
NEIGHBORHOOD = "Warren Ave Community"

# Safety check: if the live result count suddenly drops below this
# percentage of the previous count, assume the API may be incomplete
# and do not generate removal alerts.
MIN_RESULT_RATIO = 0.50

# The ArcGIS layer currently allows up to 2,000 records per request.
MAX_RECORDS = 2000


def fetch_live_data():
    """Fetch the current DLBA properties for the configured neighborhood."""

    params = {
        "where": f"neighborhood = '{NEIGHBORHOOD}'",
        "outFields": "name,parcel_id,inventory_status_socrata,neighborhood",
        "returnGeometry": "false",
        "resultRecordCount": MAX_RECORDS,
        "f": "json",
    }

    try:
        response = requests.get(API_URL, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()

        # ArcGIS can return an error payload with HTTP 200.
        if "error" in data:
            print(f"ArcGIS API error: {data['error']}")
            return None

        # If ArcGIS says the transfer limit was exceeded, the response
        # may be incomplete. Do not treat missing records as removals.
        if data.get("exceededTransferLimit"):
            print(
                "ArcGIS returned more records than could be transferred. "
                "Aborting to avoid false removal alerts."
            )
            return None

        features = data.get("features")

        if features is None:
            print("ArcGIS response did not contain a 'features' field.")
            return None

        live_records = {}

        for feature in features:
            attrs = feature.get("attributes", {})

            # Normalize the ArcGIS field names into the names used
            # internally by this tracker.
            parcel = attrs.get("parcel_id")

            if not parcel:
                continue

            live_records[str(parcel)] = {
                "Parcel_Number": str(parcel),
                "Address": attrs.get("name"),
                "Inventory_Status": attrs.get("inventory_status_socrata"),
                "Neighborhood": attrs.get("neighborhood"),
            }

        print(f"ArcGIS returned {len(live_records)} properties.")
        return live_records

    except requests.RequestException as e:
        print(f"Error fetching data from ArcGIS API: {e}")
        return None

    except ValueError as e:
        print(f"Error decoding ArcGIS JSON response: {e}")
        return None

    except Exception as e:
        print(f"Unexpected error fetching ArcGIS data: {e}")
        return None


def load_previous_state():
    """Load the previously saved data state."""

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)

        except json.JSONDecodeError:
            print(
                f"Warning: {STATE_FILE} contains invalid JSON. "
                "Treating it as an empty state."
            )
            return {}

        except OSError as e:
            print(f"Error reading {STATE_FILE}: {e}")
            return {}

    return {}


def save_current_state(current_state):
    """Save the current data state to disk."""

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(current_state, f, indent=2, ensure_ascii=False)


def commit_state_to_github():
    """Commit the updated state file back to GitHub."""

    try:
        subprocess.run(
            [
                "git",
                "config",
                "--local",
                "user.email",
                "actions@github.com",
            ],
            check=True,
        )

        subprocess.run(
            [
                "git",
                "config",
                "--local",
                "user.name",
                "GitHub Action Tracker",
            ],
            check=True,
        )

        subprocess.run(
            ["git", "add", STATE_FILE],
            check=True,
        )

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )

        if status.stdout.strip():
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    "chore: update tracking state [skip ci]",
                ],
                check=True,
            )

            subprocess.run(
                ["git", "push"],
                check=True,
            )

            print("Successfully saved new state to GitHub repository.")
        else:
            print("No state changes to commit.")

    except subprocess.CalledProcessError as e:
        print(f"Failed to commit state back to repository: {e}")

    except Exception as e:
        print(f"Unexpected GitHub commit error: {e}")


def send_notification(html_content):
    """Send the alert through Brevo's transactional email API."""

    api_key = os.environ.get("BREVO_API_KEY")
    from_email = os.environ.get("BREVO_FROM_EMAIL")
    to_email = os.environ.get("NOTIFICATION_TO_EMAIL")

    if not all([api_key, from_email, to_email]):
        print(
            "Missing Brevo environment variables. "
            "Printing HTML to logs:"
        )
        print(html_content)
        return

    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key,
    }

    payload = {
        "sender": {
            "email": from_email,
            "name": "DLBA Property Tracker",
        },
        "to": [
            {
                "email": to_email,
            }
        ],
        "subject": (
            f"⚠️ DLBA Property Alert: Changes in {NEIGHBORHOOD}"
        ),
        "htmlContent": html_content,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=15,
        )

        if response.status_code in (200, 201, 202):
            print("Email sent successfully via Brevo.")
        else:
            print(
                f"Brevo API error: "
                f"{response.status_code} - {response.text}"
            )

    except requests.RequestException as e:
        print(f"Failed to send email via Brevo: {e}")

    except Exception as e:
        print(f"Unexpected Brevo error: {e}")


def main():
    print(f"Starting property scan for: {NEIGHBORHOOD}")

    live_data = fetch_live_data()

    # Never interpret an API failure as property removals.
    if live_data is None:
        print("Aborting run due to API fetch failure.")
        return

    previous_data = load_previous_state()

    # First run: establish the baseline without sending an alert.
    if not previous_data:
        print(
            "No previous state found. "
            "Initializing tracking ledger with baseline data."
        )

        save_current_state(live_data)
        commit_state_to_github()
        return

    previous_count = len(previous_data)
    live_count = len(live_data)

    # Protect against an incomplete API response causing a large number
    # of false "removed" alerts.
    if live_count == 0:
        print(
            "ArcGIS returned zero properties. "
            "Aborting to avoid false removal alerts."
        )
        return

    if live_count < previous_count * MIN_RESULT_RATIO:
        print(
            f"ArcGIS result count dropped unexpectedly: "
            f"{previous_count} -> {live_count}. "
            "Aborting to avoid false removal alerts."
        )
        return

    new_records = []
    removed_records = []
    changed_records = []

    # Find new properties and changes to existing properties.
    for parcel, live_attr in live_data.items():

        if parcel not in previous_data:
            new_records.append(live_attr)
            continue

        prev_attr = previous_data[parcel]
        changes = {}

        # The current ArcGIS dataset provides inventory status.
        if str(live_attr.get("Inventory_Status")) != str(
            prev_attr.get("Inventory_Status")
        ):
            changes["Inventory_Status"] = {
                "old": prev_attr.get("Inventory_Status"),
                "new": live_attr.get("Inventory_Status"),
            }

        # Also detect an address change.
        if str(live_attr.get("Address")) != str(
            prev_attr.get("Address")
        ):
            changes["Address"] = {
                "old": prev_attr.get("Address"),
                "new": live_attr.get("Address"),
            }

        if changes:
            changed_records.append(
                {
                    "address": live_attr.get("Address"),
                    "parcel": parcel,
                    "changes": changes,
                }
            )

    # Find properties that no longer appear in the current DLBA inventory.
    #
    # IMPORTANT: disappearance does not necessarily prove that the
    # property was sold. It may have been removed for another reason,
    # so the notification deliberately uses neutral wording.
    for parcel, prev_attr in previous_data.items():
        if parcel not in live_data:
            removed_records.append(prev_attr)

    if new_records or removed_records or changed_records:
        print("Changes detected! Synthesizing alert payload...")

        html = (
            f"<h2>DLBA Property Activity Update — "
            f"{escape(NEIGHBORHOOD)}</h2>"
        )

        if new_records:
            html += "<h3>🆕 Newly Listed Properties</h3><ul>"

            for record in new_records:
                address = escape(
                    str(record.get("Address") or "N/A")
                )
                parcel = escape(
                    str(record.get("Parcel_Number") or "N/A")
                )
                status = escape(
                    str(record.get("Inventory_Status") or "N/A")
                )

                html += (
                    f"<li><b>{address}</b> "
                    f"(Parcel: {parcel}) - "
                    f"Status: {status}</li>"
                )

            html += "</ul>"

        if changed_records:
            html += "<h3>🔄 Modified Listings</h3><ul>"

            for record in changed_records:
                address = escape(
                    str(record.get("address") or "N/A")
                )
                parcel = escape(
                    str(record.get("parcel") or "N/A")
                )

                html += (
                    f"<li><b>{address}</b> "
                    f"(Parcel: {parcel}):<ul>"
                )

                for field, values in record["changes"].items():
                    field_name = escape(str(field))
                    old_value = escape(
                        str(values.get("old") or "N/A")
                    )
                    new_value = escape(
                        str(values.get("new") or "N/A")
                    )

                    html += (
                        f"<li><code>{field_name}</code> "
                        f"changed from <b>{old_value}</b> "
                        f"to <b>{new_value}</b></li>"
                    )

                html += "</ul></li>"

            html += "</ul>"

        if removed_records:
            html += (
                "<h3>❌ Properties No Longer Appearing "
                "in DLBA Inventory</h3><ul>"
            )

            for record in removed_records:
                address = escape(
                    str(record.get("Address") or "N/A")
                )
                parcel = escape(
                    str(record.get("Parcel_Number") or "N/A")
                )
                status = escape(
                    str(record.get("Inventory_Status") or "N/A")
                )

                html += (
                    f"<li><b>{address}</b> "
                    f"(Parcel: {parcel}) - "
                    f"Previously listed status: {status}</li>"
                )

            html += "</ul>"

        send_notification(html)

    else:
        print(
            "Scan finished. Data matches perfectly with baseline. "
            "No updates needed."
        )

    # Save the current state after a successful comparison.
    save_current_state(live_data)
    commit_state_to_github()


if __name__ == "__main__":
    main()
