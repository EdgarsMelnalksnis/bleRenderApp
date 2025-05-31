from flask import Flask, render_template, request, redirect, send_file
import pandas as pd
from datetime import datetime, timedelta
import os
import json
from io import BytesIO
from fpdf import FPDF

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

app = Flask(__name__)

# === Google Drive Auth Setup ===
SCOPES = ['https://www.googleapis.com/auth/drive.file']
DRIVE_FOLDER_ID = "1NFAKM3Q66EQJAtDiVNvKmPbbwUYtB6qh"

def get_drive_service():
    from google_auth_oauthlib.flow import InstalledAppFlow
    if 'GOOGLE_CREDENTIALS_JSON' in os.environ and 'GOOGLE_TOKEN_JSON' in os.environ:
        creds_data = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])
        token_data = json.loads(os.environ['GOOGLE_TOKEN_JSON'])
        creds = Credentials.from_authorized_user_info(token_data)
    else:
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
            with open("token.json", "w") as token_file:
                token_file.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)

def find_drive_file(name):
    service = get_drive_service()
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name = '{name}' and trashed = false",
        fields="files(id, name)"
    ).execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def download_json_from_drive(name):
    file_id = find_drive_file(name)
    if not file_id:
        return {}
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return json.load(fh)

def upload_json_to_drive(name, content):
    service = get_drive_service()
    file_id = find_drive_file(name)
    fh = BytesIO(json.dumps(content, indent=2).encode("utf-8"))
    media = MediaIoBaseUpload(fh, mimetype='application/json', resumable=True)
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {"name": name, "parents": [DRIVE_FOLDER_ID]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def get_drive_file_map():
    service = get_drive_service()
    results = service.files().list(
        q=f"'{DRIVE_FOLDER_ID}' in parents and name contains '_logs.csv' and trashed = false",
        fields="files(id, name)"
    ).execute()
    files = results.get('files', [])
    return {f['name'].split("_")[0]: f['id'] for f in files}

def download_csv_from_drive(file_id):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return pd.read_csv(fh)

def load_latest_logs_from_drive():
    log_files = get_drive_file_map()
    combined_logs = []
    for hub, file_id in log_files.items():
        try:
            df = download_csv_from_drive(file_id)
            df["hub"] = hub
            df["time"] = pd.to_datetime(df["timestamp"], unit="s")
            df = df.drop_duplicates(subset=["mac", "time", "hub", "rssi"])
            combined_logs.append(df)
        except Exception as e:
            print(f"Failed to load log for {hub}: {e}")
    if combined_logs:
        full_df = pd.concat(combined_logs).sort_values(["mac", "time"])
        full_df["time_diff"] = full_df.groupby("mac")["time"].diff().fillna(pd.Timedelta(seconds=0))
        full_df["time_diff"] = full_df["time_diff"].apply(lambda x: str(x).split('.')[0])
        return full_df
    return pd.DataFrame()

full_df = load_latest_logs_from_drive()
all_hubs = sorted(full_df["hub"].unique()) if not full_df.empty else []
valid_macs = sorted(full_df["mac"].unique()) if not full_df.empty else []
mac_name_map = download_json_from_drive("mac_names.json")
hub_name_map = download_json_from_drive("hub_names.json")

def filter_by_time(df, time_filter, from_date=None, to_date=None, from_time=None, to_time=None):
    now = datetime.now()
    def combine_date_time(date_str, time_str, default_time):
        try:
            base = datetime.strptime(date_str, "%Y-%m-%d")
            t = datetime.strptime(time_str, "%H:%M").time() if time_str else default_time
            return datetime.combine(base.date(), t)
        except:
            return None

    if time_filter == "custom":
        start = combine_date_time(from_date, from_time, datetime.min.time()) if from_date else df["time"].min()
        end = combine_date_time(to_date, to_time, datetime.max.time()) if to_date else df["time"].max()
        return df[(df["time"] >= start) & (df["time"] <= end)]
    elif time_filter == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return df[df["time"] >= start]
    elif time_filter == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return df[(df["time"] >= start) & (df["time"] < end)]
    elif time_filter == "this_week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return df[df["time"] >= start]
    elif time_filter == "last_week":
        start = now - timedelta(days=now.weekday() + 7)
        end = start + timedelta(days=7)
        return df[(df["time"] >= start) & (df["time"] < end)]
    return df

def compute_mac_stats():
    stats = {}
    today = datetime.now().date()
    for mac in valid_macs:
        mac_df = full_df[full_df["mac"] == mac]
        last_seen = mac_df["time"].max()
        today_count = mac_df[mac_df["time"].dt.date == today].shape[0]
        total_count = mac_df.shape[0]
        stats[mac] = {
            "name": mac_name_map.get(mac, ""),
            "last_seen": last_seen.strftime("%Y-%m-%d %H:%M:%S") if pd.notnull(last_seen) else "—",
            "today_count": today_count,
            "total_count": total_count
        }
    return stats

def compute_hub_stats():
    stats = {}
    today = datetime.now().date()
    for hub in all_hubs:
        hub_df = full_df[full_df["hub"] == hub]
        last_seen = hub_df["time"].max()
        today_count = hub_df[hub_df["time"].dt.date == today].shape[0]
        total_count = hub_df.shape[0]
        stats[hub] = {
            "name": hub_name_map.get(hub, ""),
            "last_seen": last_seen.strftime("%Y-%m-%d %H:%M:%S") if pd.notnull(last_seen) else "—",
            "today_count": today_count,
            "total_count": total_count
        }
    return stats

@app.route("/")
def index():
    return render_template("index.html",
        macs=valid_macs,
        hubs=all_hubs,
        mac_stats=compute_mac_stats(),
        hub_stats=compute_hub_stats()
    )

@app.route("/refresh_logs")
def refresh_logs():
    global full_df, all_hubs, valid_macs
    full_df = load_latest_logs_from_drive()
    all_hubs[:] = sorted(full_df["hub"].unique()) if not full_df.empty else []
    valid_macs[:] = sorted(full_df["mac"].unique()) if not full_df.empty else []
    return redirect("/")

@app.route("/mac")
def mac_logs():
    mac = request.args.get("mac")
    hubs = request.args.getlist("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["mac"] == mac]
    if hubs:
        df = df[df["hub"].isin(hubs)]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    return render_template("mac_logs.html",
        mac=mac,
        logs=df,
        selected_hubs=hubs,
        mac_stats=compute_mac_stats(),
        hub_stats=compute_hub_stats()
    )

@app.route("/mac/download")
def download_mac_logs():
    mac = request.args.get("mac")
    hubs = request.args.getlist("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["mac"] == mac]
    if hubs:
        df = df[df["hub"].isin(hubs)]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    output = BytesIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"{mac}_logs.csv", mimetype="text/csv")

@app.route("/mac/download_pdf")
def download_mac_logs_pdf():
    mac = request.args.get("mac")
    hubs = request.args.getlist("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["mac"] == mac]
    if hubs:
        df = df[df["hub"].isin(hubs)]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=10)
    pdf.cell(200, 10, txt=f"MAC: {mac} Log Export", ln=True, align='L')

    columns = df.columns.tolist()
    pdf.set_font("Arial", size=8)
    col_header = " | ".join(columns)
    pdf.multi_cell(0, 5, col_header)

    for _, row in df.iterrows():
        values = [str(row[col])[:20].replace("\n", " ").replace("|", "/") for col in columns]
        line = " | ".join(values)
        if pdf.get_string_width(line) > 190:
            line = line[:180] + "..."
        pdf.multi_cell(0, 5, line)

    output = BytesIO()
    pdf_bytes = pdf.output(dest='S').encode('latin1')
    output.write(pdf_bytes)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"{mac}_logs.pdf", mimetype="application/pdf")

@app.route("/hub/download")
def download_hub_logs():
    hub = request.args.get("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["hub"] == hub]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    output = BytesIO()
    df.to_csv(output, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"{hub}_logs.csv", mimetype="text/csv")

@app.route("/hub/download_pdf")
def download_hub_logs_pdf():
    hub = request.args.get("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["hub"] == hub]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=10)
    pdf.cell(200, 10, txt=f"HUB: {hub} Log Export", ln=True, align='L')

    columns = df.columns.tolist()
    pdf.set_font("Arial", size=8)
    col_header = " | ".join(columns)
    pdf.multi_cell(0, 5, col_header)

    for _, row in df.iterrows():
        values = [str(row[col])[:20].replace("\n", " ").replace("|", "/") for col in columns]
        line = " | ".join(values)
        if pdf.get_string_width(line) > 190:
            line = line[:180] + "..."
        pdf.multi_cell(0, 5, line)

    output = BytesIO()
    pdf_bytes = pdf.output(dest='S').encode('latin1')
    output.write(pdf_bytes)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"{hub}_logs.pdf", mimetype="application/pdf")


@app.route("/hub")
def hub_logs():
    hub = request.args.get("hub")
    time_filter = request.args.get("time_filter")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    from_time = request.args.get("from_time")
    to_time = request.args.get("to_time")

    df = full_df[full_df["hub"] == hub]
    df = filter_by_time(df, time_filter, from_date, to_date, from_time, to_time)
    df = df.sort_values("time")

    return render_template("hub_logs.html",
        hub=hub,
        logs=df,
        mac_stats=compute_mac_stats(),
        hub_stats=compute_hub_stats()
    )

@app.route("/rename", methods=["POST"])
def rename():
    for key, value in request.form.items():
        if key.startswith("rename_"):
            mac = key.replace("rename_", "")
            mac_name_map[mac] = value.strip()
    upload_json_to_drive("mac_names.json", mac_name_map)
    return redirect("/")

@app.route("/rename_hubs", methods=["POST"])
def rename_hubs():
    for key, value in request.form.items():
        if key.startswith("renamehub_"):
            hub = key.replace("renamehub_", "")
            hub_name_map[hub] = value.strip()
    upload_json_to_drive("hub_names.json", hub_name_map)
    return redirect("/")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)

