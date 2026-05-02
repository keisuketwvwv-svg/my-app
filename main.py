#!/usr/bin/env python3
"""
PDF Auto-Summarizer
Reads PDFs from a Google Drive folder, generates summaries and TODOs with Claude,
and creates a Gmail draft email with the results.

Usage:
    python main.py --folder-id <DRIVE_FOLDER_ID> --email <RECIPIENT_EMAIL>

Setup:
    1. Place credentials/credentials.json (Google OAuth2 client secret)
    2. Set ANTHROPIC_API_KEY in .env or environment
    3. Run once to complete Google OAuth flow (opens browser)
"""

import os
import io
import json
import base64
import argparse
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
import pdfplumber
import anthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

SYSTEM_PROMPT = """\
You are an expert document analyst specializing in extracting actionable insights \
from business and technical documents.

For each PDF document provided, analyze its content and produce:
1. A concise executive summary (3–5 sentences capturing the main purpose and findings)
2. A prioritized list of concrete action items / TODOs mentioned or implied by the document
3. Key insights and important points worth highlighting

Be specific and actionable. Focus on what matters most to the reader.\
"""

# JSON schema for structured Claude output
_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Executive summary in 3-5 sentences",
        },
        "todos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actionable items / TODOs from the document",
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Key insights and important points",
        },
    },
    "required": ["summary", "todos", "key_points"],
    "additionalProperties": False,
}


# ─── Google Authentication ────────────────────────────────────────────────────

def get_google_credentials() -> Credentials:
    """Return valid Google OAuth2 credentials, refreshing or re-authorising as needed."""
    token_path = Path("credentials/token.json")
    creds_path = Path("credentials/credentials.json")

    if not creds_path.exists():
        raise FileNotFoundError(
            "Google credentials not found.\n"
            "Download your OAuth2 client secret from "
            "https://console.cloud.google.com/apis/credentials and save it to "
            "credentials/credentials.json"
        )

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    return creds


# ─── Google Drive ─────────────────────────────────────────────────────────────

def list_pdfs_in_folder(drive_service, folder_id: str) -> list[dict]:
    """Return metadata for every non-trashed PDF inside `folder_id`."""
    result = drive_service.files().list(
        q=(
            f"'{folder_id}' in parents"
            " and mimeType='application/pdf'"
            " and trashed=false"
        ),
        fields="files(id, name, size, modifiedTime)",
        orderBy="modifiedTime desc",
    ).execute()
    return result.get("files", [])


def download_pdf(drive_service, file_id: str) -> bytes:
    """Download a Drive file and return its raw bytes."""
    request = drive_service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract plain text from a PDF's bytes using pdfplumber."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = [page.extract_text() for page in pdf.pages if page.extract_text()]
    return "\n\n".join(pages)


# ─── Claude Analysis ──────────────────────────────────────────────────────────

def analyze_pdf_with_claude(
    client: anthropic.Anthropic,
    filename: str,
    content: str,
) -> dict:
    """Send PDF text to Claude and return a structured analysis dict."""
    # Stay well within the context window for long documents
    truncated = content[:50_000] if len(content) > 50_000 else content

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        # Cache the stable system prompt across all PDF calls in this run
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": _ANALYSIS_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": f"Analyze this document: **{filename}**\n\n{truncated}",
            }
        ],
    )

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ─── Gmail ────────────────────────────────────────────────────────────────────

def _render_email_html(analyses: list[dict]) -> str:
    """Build a styled HTML email body from a list of analysis dicts."""
    sections = ""
    for a in analyses:
        todos_li = "".join(f"<li>{t}</li>" for t in a.get("todos", []))
        points_li = "".join(f"<li>{p}</li>" for p in a.get("key_points", []))

        todos_block = (
            f"<h3 style='margin:16px 0 8px;color:#d93025;font-size:14px;'>"
            f"✅ Action Items</h3><ul style='margin:0;padding-left:20px;'>{todos_li}</ul>"
            if todos_li
            else ""
        )
        points_block = (
            f"<h3 style='margin:16px 0 8px;color:#7b1fa2;font-size:14px;'>"
            f"💡 Key Points</h3><ul style='margin:0;padding-left:20px;'>{points_li}</ul>"
            if points_li
            else ""
        )

        sections += f"""
        <div style="margin-bottom:28px;padding:20px;border:1px solid #e0e0e0;
                    border-radius:8px;background:#fafafa;">
          <h2 style="margin:0 0 12px;color:#1a73e8;font-size:17px;">{a['filename']}</h2>
          <h3 style="margin:0 0 8px;color:#188038;font-size:14px;">📝 Summary</h3>
          <p style="margin:0;line-height:1.65;">{a.get('summary', '—')}</p>
          {todos_block}
          {points_block}
        </div>"""

    return f"""\
<html>
<body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;
             padding:24px;color:#202124;">
  <h1 style="color:#202124;font-size:22px;margin-bottom:4px;">📄 PDF Summary Report</h1>
  <p style="color:#5f6368;margin-top:0;">{len(analyses)} document(s) analysed</p>
  <hr style="border:none;border-top:1px solid #e0e0e0;margin:20px 0;">
  {sections}
  <hr style="border:none;border-top:1px solid #e0e0e0;margin:20px 0;">
  <p style="color:#9aa0a6;font-size:12px;">
    Generated automatically by PDF Auto-Summarizer
  </p>
</body>
</html>"""


def create_gmail_draft(
    gmail_service,
    to: str,
    subject: str,
    html_body: str,
) -> str:
    """Create a Gmail draft and return its ID."""
    msg = MIMEMultipart("alternative")
    msg["to"] = to
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = gmail_service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()
    return draft["id"]


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarise PDFs from Google Drive and create a Gmail draft"
    )
    parser.add_argument("--folder-id", required=True, help="Google Drive folder ID")
    parser.add_argument("--email", required=True, help="Recipient email address for the draft")
    parser.add_argument(
        "--subject",
        default="PDF Summary Report",
        help="Email subject line (default: 'PDF Summary Report')",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set. Add it to .env or your environment.")

    # ── Auth ──────────────────────────────────────────────────────────────────
    print("🔑 Authenticating with Google…")
    creds = get_google_credentials()
    drive_svc = build("drive", "v3", credentials=creds)
    gmail_svc = build("gmail", "v1", credentials=creds)
    claude = anthropic.Anthropic(api_key=api_key)

    # ── Discover PDFs ─────────────────────────────────────────────────────────
    print(f"\n🔍 Searching for PDFs in folder: {args.folder_id}")
    pdfs = list_pdfs_in_folder(drive_svc, args.folder_id)

    if not pdfs:
        print("❌ No PDFs found in the specified folder.")
        return

    print(f"📂 Found {len(pdfs)} PDF(s):\n")
    for pdf in pdfs:
        size_kb = int(pdf.get("size", 0)) // 1024
        print(f"   • {pdf['name']}  ({size_kb} KB)")

    # ── Process each PDF ──────────────────────────────────────────────────────
    analyses: list[dict] = []
    for i, pdf in enumerate(pdfs, 1):
        print(f"\n[{i}/{len(pdfs)}] {pdf['name']}")

        print("   ↓  Downloading…")
        pdf_bytes = download_pdf(drive_svc, pdf["id"])

        print("   📖 Extracting text…")
        text = extract_text_from_pdf(pdf_bytes)

        if not text.strip():
            print("   ⚠️  No text extracted (possibly a scanned image PDF)")
            analyses.append({
                "filename": pdf["name"],
                "summary": (
                    "Could not extract text from this PDF — "
                    "it may contain only scanned images."
                ),
                "todos": [],
                "key_points": [],
            })
            continue

        print(f"   🤖 Analysing with Claude ({len(text):,} chars)…")
        analysis = analyze_pdf_with_claude(claude, pdf["name"], text)
        analysis["filename"] = pdf["name"]
        analyses.append(analysis)
        print("   ✅ Done")

    # ── Create Gmail draft ────────────────────────────────────────────────────
    print(f"\n📧 Creating Gmail draft → {args.email}")
    html_body = _render_email_html(analyses)
    draft_id = create_gmail_draft(gmail_svc, args.email, args.subject, html_body)

    print(f"\n✨ Complete!  Draft ID: {draft_id}")
    print("   Open Gmail and check your Drafts folder.")


if __name__ == "__main__":
    main()
