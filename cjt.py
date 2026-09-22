import os
import time
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ----------------- CONFIGURATION -----------------
REPO_OWNER = "SimplifyJobs"
REPO_NAME = "Summer2027-Internships"
BRANCH = "dev"
CHECK_INTERVAL_SECONDS = 60
LAST_COMMIT_FILE = "last_commit.txt"
PROFILE_FILE = "profile.md"

# Credentials from environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Initialize the Gemini client (picks up GEMINI_API_KEY environment variable)
client = genai.Client()

HEADERS = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


def send_telegram_alert(message_text: str):
    """Sends push notification directly via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "disable_web_page_preview": False
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"[!] Telegram API error ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"[!] Failed to send Telegram alert: {e}")


def get_latest_commit():
    """Polls GitHub API for the latest commit SHA and commit message."""
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/{BRANCH}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            return data.get("sha"), data.get("commit", {}).get("message", "")
        elif res.status_code == 403:
            print("[!] GitHub API rate limit hit. Ensure GITHUB_TOKEN is valid.")
    except Exception as e:
        print(f"[!] GitHub polling error: {e}")
    return None, None


def get_commit_diff(sha: str):
    """Fetches the raw git diff for a specific commit."""
    diff_headers = HEADERS.copy()
    diff_headers["Accept"] = "application/vnd.github.v3.diff"
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/{sha}"
    try:
        res = requests.get(url, headers=diff_headers, timeout=15)
        if res.status_code == 200:
            return res.text
    except Exception as e:
        print(f"[!] GitHub diff fetch error: {e}")
    return None


def evaluate_and_notify(diff_text: str):
    """Filters diff lines, evaluates matches via Gemini, and fires Telegram alerts."""
    # Extract only newly added markdown table rows
    added_lines = "\n".join([
        line for line in diff_text.splitlines()
        if line.startswith("+|") and not line.startswith("+++")
    ])
    if not added_lines.strip():
        print("[-] Diff had no newly added table rows. Skipping.")
        return

    # Load candidate rubric
    profile_content = ""
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            profile_content = f.read()
    else:
        profile_content = (
            "Target: Software Engineering Intern 2027 (React, Next.js, TypeScript, Python, SQL, AWS). "
            "Disqualify Master's/PhD-only and Senior/Lead roles."
        )

    prompt = (
        f"""Read the candidate profile and evaluate the following newly added internship table rows from GitHub:
        Candidate Profile:
        {profile_content}

        Newly Added Postings (Git diff rows):
        {added_lines[:6000]}

        Strict Evaluation Rules:
        1. Disqualify any role that requires enrollment in a Master's or PhD program (often indicated by '🎓' or explicitly stating MS/PhD).
        2. For each qualifying match, calculate a percent match (0-100%) against the profile.
        3. If the calculated percent match is below 65%, DO NOT include it.
        4. If multiple roles qualify, separate each job posting block strictly with a line containing only '---'. Format each job block EXACTLY like this:

        [Match %]% -- [Company] -- [Role Title]
        📍 Location: [Location]
        🔗 Link: [Application URL]
        💲 Salary: [Estimated salary, example: "Estimated: $45-$55/hr based on ..."]

        If NO roles qualify with >=65% match, return strictly: NO_MATCH
        """
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
        )
        output = response.text.strip()
        if output and "NO_MATCH" not in output:
            # Split into individual job blocks
            job_alerts = [job.strip() for job in output.split("---") if job.strip()]
            print(f"[+] Found {len(job_alerts)} qualifying match(es). Sending notifications...")
            for alert in job_alerts:
                send_telegram_alert(alert)
                time.sleep(1)  # Brief delay to respect Telegram rate limits and preserve delivery order
        else:
            print("[-] Roles evaluated, but none met the >=65% threshold.")
    except Exception as e:
        print(f"[!] Gemini evaluation error: {e}")


def main():
    print(f"[*] Starting GCP cloud tracker for https://github.com/{REPO_OWNER}/{REPO_NAME} on '{BRANCH}'...")
    last_sha = None
    if os.path.exists(LAST_COMMIT_FILE):
        try:
            with open(LAST_COMMIT_FILE, "r") as f:
                last_sha = f.read().strip()
                print(f"[*] Resuming from last recorded commit: {last_sha[:7]}")
        except Exception:
            pass

    while True:
        current_sha, message = get_latest_commit()
        if current_sha and current_sha != last_sha:
            if "Updating READMEs" in message:
                print(f"[+] Found README update commit: {current_sha[:7]} - '{message.splitlines()[0]}'")
                diff = get_commit_diff(current_sha)
                if diff:
                    evaluate_and_notify(diff)
            else:
                print(f"[i] Skipping non-README commit {current_sha[:7]}: '{message.splitlines()[0]}'")

            last_sha = current_sha
            with open(LAST_COMMIT_FILE, "w") as f:
                f.write(current_sha)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()