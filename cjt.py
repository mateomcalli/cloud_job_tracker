import os
import time
import requests
import re
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
SEEN_JOBS_FILE = "seen_jobs.txt"

# Credentials from environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Initialize the Gemini client
client = genai.Client()

HEADERS = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


def load_seen_jobs():
    """Loads previously seen compound job keys (Company - Role - Location)."""
    if os.path.exists(SEEN_JOBS_FILE):
        try:
            with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
                return set(line.strip() for line in f if line.strip())
        except Exception as e:
            print(f"[!] Error reading {SEEN_JOBS_FILE}: {e}")
    return set()


def save_seen_jobs(seen_set):
    """Persists seen compound job keys to disk."""
    try:
        with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
            for item in sorted(seen_set):
                f.write(f"{item}\n")
    except Exception as e:
        print(f"[!] Error writing {SEEN_JOBS_FILE}: {e}")


def send_telegram_alert(message_text: str):
    """Sends push notification directly via Telegram Bot API with retry handling."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "disable_web_page_preview": True
    }
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=25)
            if response.status_code == 200:
                return
            elif response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 3)
                time.sleep(retry_after)
            else:
                print(f"[!] Telegram API error ({response.status_code}): {response.text}")
                return
        except Exception as e:
            if attempt == 2:
                print(f"[!] Failed to send Telegram alert after 3 attempts: {e}")
            time.sleep(2)


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


def get_readme_at_commit(sha: str):
    """Fetches the complete README.md content exactly as it existed at a given commit."""
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{sha}/README.md"
    headers = HEADERS.copy()
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            return res.text
    except Exception as e:
        print(f"[!] Error fetching README at {sha}: {e}")
    return None


def extract_all_jobs_from_readme(readme_text: str):
    """Parses clean, complete job entries directly from README HTML tables."""
    jobs = []
    last_company = "Unknown Company"

    rows = re.findall(r"<tr>(.*?)</tr>", readme_text, re.DOTALL)
    for row in rows:
        tds = re.findall(r"<td.*?>(.*?)</td>", row, re.DOTALL)
        if len(tds) < 3:
            continue

        def clean(html_str):
            txt = re.sub(r"<.*?>", " ", html_str)
            return " ".join(txt.split())

        col0 = clean(tds[0])
        col1 = clean(tds[1])
        location = clean(tds[2]) if len(tds) > 2 else "Unknown"

        is_subrole = "↳" in col0 or "↳" in col1

        if is_subrole:
            company = last_company
            role = col1.replace("↳", "").strip() if "↳" in col0 else col0.replace("↳", "").strip()
        else:
            raw_comp = col0.replace("🔥", "").strip()
            if raw_comp and "location" not in raw_comp.lower():
                last_company = raw_comp
            company = last_company
            role = col1

        # Extract direct application link
        app_url = "https://github.com/SimplifyJobs/Summer2027-Internships"
        links = re.findall(r'href="([^"]+)"', row)
        valid_links = [l for l in links if "GHList&utm_medium=company" not in l and "simplify.jobs/p/" not in l]
        if valid_links:
            app_url = valid_links[0]

        # Extract age (e.g. 0d, 1d)
        age = clean(tds[-1]) if tds else ""

        if company and role:
            jobs.append({
                "company": company,
                "role": role,
                "location": location,
                "link": app_url,
                "age": age,
                "raw_text": f"{company} | {role} | {location} | Link: {app_url} | {age}"
            })

    return jobs


def evaluate_and_notify(commit_sha: str):
    """Fetches full commit README, deduplicates by Company-Role-Location, and evaluates new 0d roles."""
    readme_text = get_readme_at_commit(commit_sha)
    if not readme_text:
        print(f"[-] Could not load README for commit {commit_sha}.")
        return

    all_jobs = extract_all_jobs_from_readme(readme_text)
    seen_jobs = load_seen_jobs()
    new_jobs = []

    for j in all_jobs:
        # Compound key supports multiple locations for identical roles
        job_key = f"{j['company']} - {j['role']} - {j['location']}"
        
        if job_key not in seen_jobs:
            if "0d" in j["age"]:
                new_jobs.append(j["raw_text"])
            seen_jobs.add(job_key)

    save_seen_jobs(seen_jobs)

    if not new_jobs:
        print("[-] README updated, but no unseen 0d roles found.")
        return

    print(f"[+] Found {len(new_jobs)} new 0d role(s). Evaluating with Gemini...")
    for idx, job in enumerate(new_jobs, start=1):
        print(f"    {idx}. {job[:120]}")

    profile_content = ""
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                profile_content = f.read()
        except Exception:
            pass

    prompt = ( 
        f"""Read the candidate profile and evaluate the following newly added internship table rows from GitHub:
        Candidate Profile:
        {profile_content}

        Newly Added Postings:
        {"\n".join(new_jobs[:50])}

        Strict Evaluation Rules:
        1. Disqualify any role requiring enrollment in a Master's or PhD program (often indicated by '🎓' or explicitly stating MS/PhD).
        2. General Software Engineer Intern, Backend Intern, Full Stack Intern, or Systems Intern positions are strong matches (>=75%) for an undergraduate Computer Science profile.
        3. Calculate a percent match (0-100%) against the candidate profile. Disqualify any role below 65%.
        4. Separate multiple qualifying roles strictly with a single line containing only '---'. Format each job block EXACTLY like this, and always include salary:

        [Match %]% -- [Company] -- [Role Title]
        📍 Location: [Location]
        🔗 Link: [Application URL]
        💲 Salary: [Estimated salary, example: "Estimated: $45-$55/hr based on market"]

        If NO roles qualify with >=65% match, return strictly: NO_MATCH
        """
    )
    
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                tools=[]
            )
        )
        output = response.text.strip()
        print("\n" + "="*20 + " GEMINI RAW RESPONSE " + "="*20)
        print(output)
        print("="*61 + "\n")

        if output and "NO_MATCH" not in output:
            job_alerts = [job.strip() for job in output.split("---") if job.strip()]
            print(f"[+] Found {len(job_alerts)} qualifying match(es). Sending notifications...")
            for alert in job_alerts:
                send_telegram_alert(alert)
                time.sleep(1)
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
            summary_msg = message.splitlines()[0] if message else "No commit message"
            print(f"[+] New commit detected: {current_sha[:7]} - '{summary_msg}'")
            evaluate_and_notify(current_sha)

            last_sha = current_sha
            with open(LAST_COMMIT_FILE, "w") as f:
                f.write(current_sha)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()