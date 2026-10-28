# GitHub-to-OneDrive updates

After setup, every push to `main` builds a code-only `latest.zip` in GitHub Actions,
uploads it to `Documents/Quickbook App Updates` in OneDrive, and refreshes
`release.json`. The app checks that manifest when `Start Dashboard.cmd` runs.
Each workstation keeps its own `config.json`, Google credentials, and SQLite
history. The colleague does not need GitHub or OneDrive access.

## One-time Microsoft and GitHub setup

The GitHub runner must authenticate to Microsoft Graph. A shared OneDrive browser
link grants download access; it does not grant GitHub upload access.

1. In Microsoft Entra ID, register an app for this automation and add the
   Microsoft Graph **Application** permission `Files.ReadWrite.All`, then grant
   tenant admin consent. The Graph upload and share-link APIs document this
   permission for app-only access. This permission is broad across the tenant's
   files; only proceed if the tenant administrator accepts that scope.
2. Add a federated credential to that app:
   - Issuer: `https://token.actions.githubusercontent.com`
   - Audience: `api://AzureADTokenExchange`
   - Subject: `repo:basitmckinneyandco-wq/QuickBook-Automation:ref:refs/heads/main`
3. In GitHub, open the repository's **Settings → Secrets and variables → Actions**
   and add repository secrets `AZURE_CLIENT_ID` and `AZURE_TENANT_ID` from the
   app registration and tenant. Add repository variable
   `QB_ONEDRIVE_PUBLISH_ENABLED` with value `true` to turn on automatic publishing.
4. Confirm the OneDrive owner can create **Anyone with the link** view links.
   The workflow creates stable view links for `latest.zip` and `release.json`.

Microsoft Graph's application permission for uploading/replacing files and
creating their sharing links is documented at:

- <https://learn.microsoft.com/en-us/graph/api/driveitem-put-content?view=graph-rest-1.0>
- <https://learn.microsoft.com/en-us/graph/api/driveitem-createlink?view=graph-rest-1.0>

## One-time workstation setup

After the workflow runs successfully, copy the manifest link shown in its run
summary into `update_config.json` beside `Start Dashboard.cmd` on each existing
workstation, using `update_config.example.json` as the shape. Then the colleague
starts the app normally; future updates arrive when the dashboard is next
started. They never receive a GitHub or OneDrive login.

The first user setup still needs the app folder and updater files installed once.
The updater applies only committed application code and verifies the package
hash before replacing files.
