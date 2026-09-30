# Connect Google: the one-time Google Cloud client

Keryx signs in to your Google account with an OAuth client of your own. You make it once,
in about five minutes, and every Google sign-in after that uses it.

1. **Create a project** at <https://console.cloud.google.com/projectcreate>. Call it
   "Keryx", or anything you will recognise.
2. **Enable the APIs** it will use, in that project:
   - the Gmail API: <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
   - the Google Calendar API: <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
3. **Branding and audience.** Open Google Auth Platform
   (<https://console.cloud.google.com/auth/overview>) and get started: choose
   **External**, and give your own email address as the support contact. Under
   **Audience**, add your own address as a test user.
4. **Publish it.** Still under **Audience**, choose **Publish app → In production**. Left
   in Testing, the sign-in stops working after seven days. An unverified app is fine for
   your own account: Google shows one warning, and you click through it.
5. **Create the client.** Under **Clients**, choose **Create client → Desktop app**, then
   **Download JSON**.
6. **Hand it to Keryx.** Give the path of that downloaded file below (dragging it into
   the terminal types the path). Keryx checks it, copies it to
   `~/.config/keryx/google_client_secret.json` readable by you alone, and uses it from then
   on; you may delete the download.

From the command line instead: `keryx auth login gmail --client-file <that file>`.
