# Google Sheets Test Setup

This guide walks through setting up a Google Sheets test spreadsheet so you
can run integration tests against the Sheets sync features.

---

## 1. Create a Google Cloud project (or reuse an existing one)

1. Go to [console.cloud.google.com](https://console.cloud.google.com/).
2. Create a new project or select an existing one.

## 2. Enable the Google Sheets API

1. In the Cloud Console, go to **APIs & Services > Library**.
2. Search for **Google Sheets API** and click **Enable**.
3. Also enable the **Google Drive API** (needed for sharing/permissions).

## 3. Create a service account

1. Go to **APIs & Services > Credentials**.
2. Click **Create Credentials > Service account**.
3. Give it a name (e.g., `lora-cp-test`).
4. Skip optional role assignments; click **Done**.
5. Click on the new service account, go to the **Keys** tab.
6. Click **Add Key > Create new key > JSON**.
7. Save the downloaded JSON file. This is your service account key.

## 4. Configure the application

You have two options for providing the service account credentials:

**Option A: File path**

Place the JSON key file in the project root (e.g., `google_sa.json`) and set:

```bash
export GOOGLE_SERVICE_ACCOUNT_FILE=google_sa.json
```

**Option B: Raw JSON string**

Set the entire JSON content as an environment variable:

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account","project_id":"...","private_key":"..."}'
```

Option B is useful in CI/CD where you cannot write files.

## 5. Create a test spreadsheet

1. Go to [sheets.google.com](https://sheets.google.com/) and create a new
   spreadsheet.
2. Name it something like `LoRa-CP Test Spreadsheet`.
3. Note the spreadsheet ID from the URL:
   ```
   https://docs.google.com/spreadsheets/d/SPREADSHEET_ID_HERE/edit
   ```

## 6. Share the spreadsheet with the service account

1. Open the spreadsheet in Google Sheets.
2. Click **Share**.
3. Paste the service account email address (found in the JSON key file under
   `client_email`, e.g., `lora-cp-test@my-project.iam.gserviceaccount.com`).
4. Grant **Editor** access.
5. Click **Send** (uncheck "Notify people" if you prefer).

## 7. Set the test environment variable

```bash
export TEST_SPREADSHEET_ID="your-spreadsheet-id-here"
```

For local development, add this to your `.env` file:

```
TEST_SPREADSHEET_ID=your-spreadsheet-id-here
```

You can also set the default spreadsheet for the app:

```bash
export GOOGLE_SHEETS_SPREADSHEET_ID="your-spreadsheet-id-here"
```

## 8. Verify the setup

Start the app and log in as admin:

```bash
make run
```

1. Navigate to `/sheets` in the web UI.
2. Paste the spreadsheet ID into the top field.
3. Try the **Build Wizard** to generate checkpoint tabs.
4. Check the spreadsheet -- you should see new tabs created.

### Quick API test

```bash
# Log in
curl -c cookies.txt -X POST http://localhost:5001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# The Sheets features are accessed through the web UI at /sheets.
# There is no direct JSON API for Sheets operations.
```

---

## Environment Variables Summary

| Variable | Purpose |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the service account JSON key file |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Raw JSON string (alternative to file) |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Default spreadsheet ID for the app |
| `TEST_SPREADSHEET_ID` | Spreadsheet ID for integration tests |
| `SHEETS_SYNC_ENABLED` | `true`/`false` -- toggle automatic sync |

---

## Troubleshooting

**"Could not open service account file"**
Check that `GOOGLE_SERVICE_ACCOUNT_FILE` points to a valid path relative
to the project root, or use `GOOGLE_SERVICE_ACCOUNT_JSON` instead.

**"Insufficient Permission" or 403 errors**
- Make sure the Google Sheets API and Google Drive API are both enabled.
- Verify the spreadsheet is shared with the service account email as Editor.

**"Spreadsheet not found" or 404 errors**
- Double-check the spreadsheet ID (it is the long string in the URL, not
  the spreadsheet name).
- Confirm the spreadsheet is shared with the service account.

**Tests skip Sheets-related tests**
Most Sheets tests require a live spreadsheet. If `TEST_SPREADSHEET_ID` is
not set, these tests are skipped automatically.

---

## Security Notes

- **Never commit** the service account JSON key file to version control.
  The `.gitignore` should already exclude `*.json` key files, but verify.
- In CI, store the JSON content as a secret environment variable
  (`GOOGLE_SERVICE_ACCOUNT_JSON`).
- The service account only has access to spreadsheets explicitly shared with
  it. It cannot access your personal Google Drive.
