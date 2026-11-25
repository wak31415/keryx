# Connect Google with a client of your own

Keryx signs in to Google through an OAuth client, and that client is always your own.
Google's API Terms say "Developer credentials may not be embedded in open source projects"
(<https://developers.google.com/terms>, section 4(b)), so an open-source app like Keryx
cannot ship a shared one. You make yours once, in a Google Cloud project that is yours, and
every Google sign-in after that uses it.

`keryx auth login google` (and `keryx setup`) does this with you: it opens the page below,
shows the clicks, and asks for the path of the file Google downloads. Typing `other` at
that question shows the second way, for an account that cannot create a project. What
follows is both written out.

If you made a client with an earlier version of this guide, it still works, but its
project may have only the Gmail and Calendar APIs turned on. Turn on the rest in one
click (step 2 of the second way), or the Drive, Docs, Sheets and Slides tools will answer
that their API is not enabled.

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
6. **Hand it to Keryx.** Give the path of that file when Keryx asks. Enter takes the newest
   `client_secret_*.json` in your Downloads folder, and a folder takes the newest one in
   it. If you downloaded it on another machine, copy it to this one first, or type `paste`
   and paste what is in it, hidden. Keryx checks it, copies it to
   `~/.config/keryx/google_client_secret.json` readable by you alone, and uses it from then
   on; you may delete the download.

If Google says it could not turn an API on, or an agent later reports that one is not
enabled, turn them on yourself with the link in step 2 below.

## The second way: an account that cannot create a project

Many work and school accounts cannot create a Google Cloud project, and Personal mode
admits only the account that made the app. So make the app with a personal Google account,
any account that can create projects, and sign in to it with the account Keryx should act
as. The app is **published**: left in Testing, every sign-in ends after seven days.
Published and unverified, Google shows one warning when you sign in, and allows up to 100
accounts, which one owner never reaches.

1. **Create a project**, in a browser signed in to the personal account:
   <https://console.cloud.google.com/projectcreate>. Call it "Keryx", or anything you will
   recognise.
2. **Turn on the six APIs** Keryx uses, in one click, with that project selected at the
   top of the page:
   <https://console.cloud.google.com/apis/enableflow?apiid=gmail.googleapis.com,calendar-json.googleapis.com,drive.googleapis.com,docs.googleapis.com,sheets.googleapis.com,slides.googleapis.com>
3. **Set up the app.** Open Google Auth Platform (<https://console.cloud.google.com/auth/overview>)
   and choose **Get started**: any app name, your email for support, Audience
   **External**, a contact email, then agree to the Google API Services User Data Policy
   and choose **Create**.
4. **Publish it.** Under **Audience** (<https://console.cloud.google.com/auth/audience>),
   choose **Publish app**, so that it is **In production**. No test users are needed.
5. **Make the client.** Under **Clients** (<https://console.cloud.google.com/auth/clients>),
   choose **Create client**, then **Desktop app**, any name, **Create**, and **Download
   JSON** straight away: Google shows the secret only once.
6. **Hand it to Keryx**, as in step 6 above, then sign in with the account Keryx should act
   as. Google says it hasn't verified the app: choose **Advanced**, then **Go to** your
   app's name.

## If your organization blocks the app

A work or school account's administrators can block apps they have not approved. Google
then shows its own page (an error such as `admin_policy_enforced`, or "Access blocked")
and never sends your browser back to Keryx, so the sign-in waits until you stop it. Only
those administrators can allow the app, through their admin console's settings for
third-party apps, and nothing on this machine can get past it. Gmail and reading Drive are
restricted scopes, which organizations often hold back first.

Without a terminal to answer at (a coding agent setting Keryx up, say), give the file on
the command line instead: `keryx auth login google --client-file <that file>`.
