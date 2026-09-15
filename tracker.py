import os
import json
import subprocess
import requests

# --- Configuration ---
API_URL = "https://arcgis.com"
STATE_FILE = "data_state.json"
NEIGHBORHOOD = "Warren Ave Community"

params = {
    'where': f"Neighborhood = '{NEIGHBORHOOD}'",
    'outFields': 'Parcel_Number,Address,Inventory_Status,Sale_Price,Property_Class',
    'f': 'json'
}

def fetch_live_data():
    """Fetches real-time data from Detroit's ArcGIS server."""
    try:
        response = requests.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        live_records = {}
        for feature in data.get('features', []):
            attrs = feature.get('attributes', {})
            parcel = attrs.get('Parcel_Number')
            if parcel:
                live_records[parcel] = attrs
        return live_records
    except Exception as e:
        print(f"Error fetching data from API: {e}")
        return None

def load_previous_state():
    """Loads yesterday's saved data state."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_current_state(current_state):
    """Saves today's data state to disk."""
    with open(STATE_FILE, 'w') as f:
        json.dump(current_state, f, indent=2)

def commit_state_to_github():
    """Commits the updated json back to GitHub so it's there tomorrow."""
    try:
        subprocess.run(["git", "config", "--local", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "--local", "user.name", "GitHub Action Tracker"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run(["git", "commit", "-m", "chore: update tracking state [skip ci]"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("Successfully saved new state to GitHub repository.")
        else:
            print("No state changes to commit.")
    except Exception as e:
        print(f"Failed to commit state back to repository: {e}")

def send_notification(html_content):
    """Sends the alert via Brevo's permanently free API."""
    api_key = os.environ.get('BREVO_API_KEY')
    from_email = os.environ.get('BREVO_FROM_EMAIL')
    to_email = os.environ.get('NOTIFICATION_TO_EMAIL')
    
    if not all([api_key, from_email, to_email]):
        print("Missing Brevo environment variables. Printing HTML to logs:")
        print(html_content)
        return

    url = "https://brevo.com"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "api-key": api_key
    }
    payload = {
        "sender": {"email": from_email, "name": "DLBA Property Tracker"},
        "to": [{"email": to_email}],
        "subject": f"⚠️ DLBA Property Alert: Changes in {NEIGHBORHOOD}",
        "htmlContent": html_content
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code in:
            print("Email sent successfully via Brevo.")
        else:
            print(f"Brevo API error: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"Failed to send email via Brevo: {e}")

def main():
    print(f"Starting property scan for: {NEIGHBORHOOD}")
    live_data = fetch_live_data()
    
    if live_data is None:
        print("Aborting run due to API fetch failure.")
        return

    previous_data = load_previous_state()
    
    if not previous_data:
        print("No previous state found. Initializing tracking ledger with baseline data.")
        save_current_state(live_data)
        commit_state_to_github()
        return

    new_records = []
    deleted_records = []
    changed_records = []

    for parcel, live_attr in live_data.items():
        if parcel not in previous_data:
            new_records.append(live_attr)
        else:
            prev_attr = previous_data[parcel]
            changes = {}
            for key in ['Inventory_Status', 'Sale_Price', 'Property_Class']:
                if str(live_attr.get(key)) != str(prev_attr.get(key)):
                    changes[key] = {"old": prev_attr.get(key), "new": live_attr.get(key)}
            
            if changes:
                changed_records.append({"address": live_attr.get('Address'), "parcel": parcel, "changes": changes})

    for parcel, prev_attr in previous_data.items():
        if parcel not in live_data:
            deleted_records.append(prev_attr)

    if new_records or deleted_records or changed_records:
        print("Changes detected! Synthesizing alert payload...")
        html = f"<h2>DLBA Property Activity Update — {NEIGHBORHOOD}</h2>"
        
        if new_records:
            html += "<h3>🆕 Newly Listed Properties</h3><ul>"
            for r in new_records:
                html += f"<li><b>{r.get('Address')}</b> (Parcel: {r.get('Parcel_Number')}) - Status: {r.get('Inventory_Status')} | Price: ${r.get('Sale_Price', 'N/A')}</li>"
            html += "</ul>"

        if changed_records:
            html += "<h3>🔄 Modified Listings</h3><ul>"
            for r in changed_records:
                html += f"<li><b>{r['address']}</b> ({r['parcel']}):<ul>"
                for field, values in r['changes'].items():
                    html += f"<li><code>{field}</code> changed from <b>{values['old']}</b> to <span style='color:green;'><b>{values['new']}</b></span></li>"
                html += "</ul></li>"
            html += "</ul>"

        if deleted_records:
            html += "<h3>❌ Removed/Sold Properties</h3><ul>"
            for r in deleted_records:
                html += f"<li><b>{r.get('Address')}</b> (Parcel: {r.get('Parcel_Number')}) - Was: {r.get('Inventory_Status')}</li>"
            html += "</ul>"

        send_notification(html)
    else:
        print("Scan finished. Data matches perfectly with baseline. No updates needed.")

    save_current_state(live_data)
    commit_state_to_github()

if __name__ == "__main__":
    main()
