import os
import pandas as pd
import streamlit as st 
with open("Format.css") as f: st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
from slack_sdk import WebClient
from dotenv import load_dotenv
from datetime import datetime
import re

load_dotenv()
slack = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
channel = os.getenv("SLACK_CHANNEL")

st.title("CSV Hours Checker → Slack")

baseline_path = os.path.join("Baseline", "expected_hours.csv")
baseline_df = pd.read_csv(baseline_path)

def parse_work_days(s):
    if not isinstance(s, str):
        return []
    s_clean = s.strip('[]').strip()
    days = [day.strip().title() for day in s_clean.split(',') if day.strip()]
    return days

def format_work_days_range(s):
    days = parse_work_days(s)
    if days:
        return f"{days[0]} - {days[-1]}"
    return "-"

def get_first_last_name(full_name):
    if not isinstance(full_name, str) or not full_name.strip():
        return "Unknown"
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1]}"
    return full_name

start_time_path = os.path.join("Input", "StartTime.csv")
start_time_uploaded = st.file_uploader("Upload StartTime CSV (optional: Agent_Status_Logs)", type="csv", key="start_time_upload")
uploaded = st.file_uploader("Upload Activity CSV (Agent_Status_Statistics)", type="csv", key="main_upload")

start_time_text = ""
start_times = {}

if start_time_uploaded:
    with open(start_time_path, "wb") as f:
        f.write(start_time_uploaded.getbuffer())
    start_time_text = "StartTime CSV uploaded and saved."

if uploaded:
    match = re.search(r'\(([^)]+)\)', uploaded.name)
    if match:
        day_date_str = match.group(1)
        work_day = day_date_str.split('_')[0].title()
        match_date = day_date_str.split('_')[1]
    else:
        now = datetime.now()
        day_date_str = now.strftime("%A_%Y-%m-%d")
        work_day = now.strftime("%A")
        match_date = now.strftime("%Y-%m-%d")
    st.markdown(start_time_text)
    st.subheader(f"Day Used: {day_date_str}")

    if os.path.exists(start_time_path):
        start_time_df = pd.read_csv(start_time_path)
        required_cols = {'date', 'target_id', 'availability_status', 'on_duty_status'}
        if required_cols.issubset(set(start_time_df.columns)):
            start_time_df['date_only'] = start_time_df['date'].astype(str).str.slice(0, 10)
            filtered_start_time_df = start_time_df[
                (start_time_df['date_only'] == match_date) &
                (start_time_df['availability_status'].str.lower() == 'available') &
                (start_time_df['on_duty_status'].str.lower() == 'available')
            ]
            filtered_start_time_df.sort_values(by=['target_id', 'date'], inplace=True)
            grouped = filtered_start_time_df.groupby('target_id')
            for target_id, group in grouped:
                time_24h_str = group.iloc[0]['date'][11:16]
                time_obj = datetime.strptime(time_24h_str, "%H:%M")
                time_12h_str = time_obj.strftime("%-I:%M %p")
                start_times[str(target_id).strip()] = time_12h_str
            start_time_text = "StartTime CSV processed."
        else:
            start_time_text = "StartTime CSV missing required columns."

    activity_df = pd.read_csv(uploaded)
    for col in ["available", "occupied", "wrapup", "Handling Other CC"]:
        if col in activity_df.columns:
            activity_df[col] = pd.to_numeric(activity_df[col], errors='coerce').fillna(0)

    activity_df["operator_id"] = activity_df["operator_id"].astype(str).str.strip()
    baseline_df["operator_id"] = baseline_df["operator_id"].astype(str).str.strip()

    activity_df["hours_worked"] = (
        activity_df.get("available", 0) +
        activity_df.get("occupied", 0) +
        activity_df.get("wrapup", 0) +
        activity_df.get("Handling Other CC", 0)
    )

    merged_df = pd.merge(
        baseline_df, activity_df,
        on="operator_id",
        suffixes=("_base", "_act"),
        how="left"
    )

    merged_df["name"] = merged_df["name_act"].combine_first(merged_df["name_base"])

    output_lines = []

    for _, row in merged_df.iterrows():
        work_days_list = [d.strip().title() for d in parse_work_days(row.get("work_days", ""))]
        work_days_range = format_work_days_range(row.get("work_days", ""))
        hours = row["hours_worked"] if not pd.isna(row["hours_worked"]) else 0
        normalized_work_day = work_day.strip().title()
        scheduled_today = normalized_work_day in work_days_list

        display_name = get_first_last_name(row["name"])
        start_time_str = start_times.get(str(row["operator_id"]).strip(), "")

        if scheduled_today:
            if hours == 0 or hours < row["minimum_hours"] or hours > row["max_hours"]:
                line = f"{display_name} {hours:.1f} / {row['expected_hours']:.1f} hours"
                if start_time_str:
                    line += f" - Start: {start_time_str}"
                output_lines.append(line)
        else:
            if hours > 0:
                line = f"{display_name} {hours:.1f} hours - {work_days_range}"
                if start_time_str:
                    line += f" - Start: {start_time_str}"
                output_lines.append(line)

    baseline_ids = set(baseline_df["operator_id"])
    extra_df = activity_df[
        (~activity_df["operator_id"].isin(baseline_ids)) & (activity_df["hours_worked"] > 0)
    ]

    for _, row in extra_df.iterrows():
        name = row.get("name")
        hours = row["hours_worked"]
        display_name = get_first_last_name(name)
        start_time_str = start_times.get(str(row["operator_id"]).strip(), "")
        line = f"{display_name} {hours:.1f} / Not in Schedule"
        if start_time_str:
            line += f" - Start: {start_time_str}"
        output_lines.append(line)

    st.subheader("Results")
    if output_lines:
        for line in output_lines:
            st.write(line)

        if st.button("Send to Slack"):
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Hours Variance Report for {day_date_str}*"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "```\n" + "\n".join(output_lines) + "\n```"}
                }
            ]
            slack.chat_postMessage(channel=channel, blocks=blocks)
            st.success(f"Posted results to {channel}")
    else:
        st.info("No entries to report.")