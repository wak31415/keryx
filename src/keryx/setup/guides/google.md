# Connect Google with a client of your own

Keryx signs in to Google through an OAuth client. This is how to make one of your own, in a
Google Cloud project that is yours. You do it once, and every Google sign-in after that
uses it.

If you made a client with an earlier version of this guide, it still works, but its
project has only the Gmail and Calendar APIs turned on. Turn on the other four from the
list under "an External app" below, or the Drive, Docs, Sheets and Slides tools will
answer that their API is not enabled.

## The short way: Personal mode

Google makes a client that only you can use, with nothing to review. A Personal mode app
needs no verification, shows no "unverified app" screen, has no cap on users, and its
sign-in does not expire after seven days. Two things to know first: only the Google account
that creates it can sign in with it (any other account is refused with
`client_user_mismatch`), and a project made this way stays Personal.

1. **Open Google's setup page**, signed in as the account Keryx should act as:
   <https://console.cloud.google.com/auth/overview/personal?apis=ws,wsmcp,tasksmcp&clientType=desktop&isAgent=true>
2. **Pick the project.** Take the Personal mode project it offers, or create a new one.
3. **Check the client.** The application type is **Desktop app**, and **Use this client
   for an AI-powered agent** is ticked. The link sets both.
4. **Agree and continue.** Tick the box to agree to the Google API Services User Data
   Policy, then choose **Continue**. Google turns on the Google Workspace APIs in the
   project and creates the client.
5. **Download it now.** In the **OAuth client created** dialog, choose **Download JSON**.
   Once you close that dialog, Google does not show the client secret again.
6. **Hand it to Keryx.** Give the path of that downloaded file below (dragging it into
   the terminal types the path). Keryx checks it, copies it to
   `~/.config/keryx/google_client_secret.json` readable by you alone, and uses it from then
   on; you may delete the download.

If Google says it could not turn an API on, or an agent later reports that one is not
enabled, enable it yourself from the list in step 2 below. If your account belongs to a
Google Workspace organization, its administrator may still have to allow the restricted
scopes, which here are Gmail and reading Drive.

## If Personal mode is not offered: an External app

Use this when the page above is not available to your account, or when a second Google
account has to sign in with the same client.

1. **Create a project** at <https://console.cloud.google.com/projectcreate>. Call it
   "Keryx", or anything you will recognise.
2. **Enable the APIs** it will use, in that project:
   - Gmail: <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
   - Google Calendar: <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
   - Google Drive: <https://console.cloud.google.com/apis/library/drive.googleapis.com>
   - Google Docs: <https://console.cloud.google.com/apis/library/docs.googleapis.com>
   - Google Sheets: <https://console.cloud.google.com/apis/library/sheets.googleapis.com>
   - Google Slides: <https://console.cloud.google.com/apis/library/slides.googleapis.com>
3. **Branding and audience.** Open Google Auth Platform
   (<https://console.cloud.google.com/auth/overview>) and get started: choose
   **External**, and give your own email address as the support contact. Under
   **Audience**, add your own address as a test user.
4. **Publish it.** Still under **Audience**, choose **Publish app → In production**. Left
   in Testing, the sign-in stops working after seven days. An unverified app is fine for
   your own account: Google shows one warning, and you choose **Advanced** and go on.
5. **Create the client.** Under **Clients**, choose **Create client → Desktop app**, then
   **Download JSON**.
6. **Hand it to Keryx**, as in step 6 above.

From the command line instead: `keryx auth login google --client-file <that file>`.
