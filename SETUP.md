# Setting this up on a new computer

> **If you were given a folder containing `QBAutomation.exe`, skip to
> [The easy way](#the-easy-way-a-ready-built-exe) at the bottom — you do not
> need Python at all.**


The dashboard runs entirely on one PC. It reads policies from the Google Sheet
and writes customers, jobs and invoices into QuickBooks Desktop on that same
machine. Nothing is hosted anywhere and no one outside the computer can reach it.

---

## 1. What the computer needs

| | |
|---|---|
| **Windows** | Required. QuickBooks Desktop is Windows-only, so the app is too. |
| **QuickBooks Desktop** | Installed, with your company file. |
| **Python 3.11 or newer** | From [python.org](https://www.python.org/downloads/). **Tick "Add python.exe to PATH"** on the first screen of the installer. |

### You do NOT need to download the QuickBooks SDK

This trips people up. The part that lets an outside program talk to QuickBooks
is called the **Request Processor**, and **QuickBooks Desktop installs it
itself**. You can see it here on any machine with QuickBooks:

```
C:\Program Files (x86)\Common Files\Intuit\QuickBooks\QBXMLRP2.dll
```

The thing you can download from Intuit — the *QuickBooks SDK* — is only
documentation, sample code and developer tools. The app never uses it. Skip it.

---

## 2. Copy the folder across

Put the project folder anywhere, for example `D:\AI\code\qb-automation`.

**Delete these before you start, if the folder you were sent still has them:**

- **Every `.db` file** (`qb_automation.db`, `Testing LLC.db`, and any other) —
  these are the app's memory of what it has already created. Keep someone
  else's and the app will believe your invoices already exist and silently skip
  them. Fresh ones are made on first run.
- **`dist\`, `build\`** — leftover build output. Not needed to run.

`config.json` **can** be copied: it carries the Google Sheet link, which saves
setting it up again. The company file path in it will be wrong for this machine,
but that is fixed on the Status page in a couple of clicks (step 5) — no file
editing.

---

## 2b. The Google key file

The dashboard keeps four columns in the sheet up to date — **QB Customer/Job**,
**QB Premium**, **QB Commission**, **QB Fees** — so anyone working that sheet
can see what has already been done, on any machine.

It signs in to Google as a *service account* to do that, using a key file:

```
service-account.json      (goes in this folder, beside Start Dashboard.cmd)
```

**It is deliberately not in the zip.** A service-account key is a password:
anyone holding it can read and write every sheet that account can reach, so it
is never committed to git and never packed into a file that gets emailed
around. Ask whoever sent you the folder to give you the key separately.

Without it everything else still works — policies load, customers and invoices
are still created — but the four status columns stop updating in both
directions.

---

## 3. Install the packages it needs

Open **PowerShell**, go to the folder, and run:

```powershell
cd "D:\AI\code\qb-automation"
pip install -r requirements.txt
```

That installs two things: `pywin32`, which is how Python talks to QuickBooks,
and `google-auth`, which is how it signs in to the sheet. Everything else the
app uses comes with Python.

---

## 4. Set up config.json

**If you were sent a `config.json`, skip this step** — the company and the sheet
link are both set from the dashboard in step 5.

Otherwise copy `config.example.json` to `config.json`. Leave everything as it
is: `mode` stays `dry_run` to begin with, which means the app does everything
except actually write to QuickBooks. That is the safe way to check your setup.

---

## 5. First run

```powershell
python -m qb_agent serve
```

Then open **http://127.0.0.1:8770** in a browser.

The first time the app contacts QuickBooks, **QuickBooks shows a permission
window**. Choose **"Yes, whenever this company file is open"**, then Continue.
This only happens once per company file. If you miss it, look for a flashing
QuickBooks window in the taskbar — the app will just sit waiting until you answer.

**Leave the PowerShell window open** — that is the app running. `Ctrl+C` stops it.

### Point it at your company and your sheet

Both are done on the **Status** page. No file editing.

**The company.** Open your company in QuickBooks Desktop first. Then under
**Company file**, pick it from the dropdown — the one tagged *open in
QuickBooks* — and press **Switch to this company**. If it is not listed, press
**Rescan**.

QuickBooks only ever serves the one company it has open, so that is the only one
you can switch to while it is running. The warning above the dropdown tells you
when the app is aimed at a different company from the one QuickBooks has open —
nothing can be sent until those two agree.

**The sheet.** Under **Where the policies come from**, press **Unlock to edit**,
paste the published link, and press **Test and save**. The link is tried before
it is kept, so a wrong one is refused with the reason and the old one stays. The
box locks itself again afterwards.

> **Windows gotcha:** don't click inside that PowerShell window. Clicking puts
> the console into selection mode and freezes whatever is printing to it. If
> things seem stuck, click the window and press `Esc`.

---

## 6. Check it before going live

On the dashboard, go to **Status** and press **Run check**. It tests every
account, template and item the invoices depend on, against your actual company
file, and tells you exactly which are missing.

A brand-new company file will fail most of them. Create whatever it names:

- Accounts: `Accounts Receivable:Premium`, `:Commission`, `:Handling Fees`
- Templates: the premium payment and commission invoice templates
- Items: `Standard`, `Non-Standard`, `Fees`
- An income account, e.g. `Commission Income`

Carrier items, Transaction Types and Sources are created by the app as needed —
you don't have to add those by hand.

---

## 7. Turn on real writes

Once the check is all green, edit `config.json`:

```json
"mode": "quickbooks",
"allow_writes": true
```

Restart the app. The badge at the top right turns red and reads **LIVE**.
From then on, Send really does create things in QuickBooks.

---

## Everyday use

```powershell
cd "D:\AI\code\qb-automation"
python -m qb_agent serve
```

Or just double-click **Start Dashboard.cmd**, which does the same thing.

Other commands:

| Command | What it does |
|---|---|
| `python -m qb_agent whoami` | Prints the company file QuickBooks has open |
| `python -m qb_agent check` | Prints the current settings and safety state |
| `python -m qb_agent serve --open` | Starts and opens the browser for you |
| `python -m qb_agent --config other.json serve` | Runs against a different settings file (note: `--config` goes *before* `serve`) |

---

## When something goes wrong

Two pages answer almost everything:

- **Status** — *can I send?* Connection details, plus Run check for the names.
- **History** — *what happened when I did?* Every action, how far it got, and
  QuickBooks' error word for word. Open an entry for the detail, or use
  **Copy details** to paste it to whoever is fixing it.

Common ones:

| Symptom | Cause |
|---|---|
| "Could not start QuickBooks" | QuickBooks has a *different* company open. Switch it in QuickBooks: File → Open Previous Company. |
| "QuickBooks does not have a company file open" — **but it is open** | The PowerShell window was started with **Run as administrator** and QuickBooks was not. Windows keeps an elevated program separate from ordinary ones, so the app cannot see QuickBooks at all. Close that window, open PowerShell normally, and start again. Never run either one as administrator. |
| "A modal dialog box is showing" | A popup is open in QuickBooks. Close it. |
| "in use by another user", or QuickBooks crashes mid-batch | **The Customer Centre is open in QuickBooks.** It holds the customer list open, and writing to that list underneath it can take QuickBooks down. Close that window before sending. The dashboard warns you when you press Send, but close it anyway as a habit. |
| "Nothing can be sent right now" on the Status page | QuickBooks has a different company open from the one the app is aimed at. Switch one of them so they match. |
| "invalid reference to QuickBooks Item / template" | A name was changed in QuickBooks. Run the Status check — it names which. |
| Nothing loads, "Not connected" | The PowerShell window was closed, or is frozen in selection mode (press `Esc`). |

---

## The easy way: a ready-built .exe

If you were handed a folder with **`QBAutomation.exe`** in it, that program has
Python built inside it. Nothing to install.

```
QuickBooks Automation\
   QBAutomation.exe          the whole app
   config.example.json       copy this to config.json and fill it in
   web\                      the dashboard pages
   Start Dashboard.cmd       double-click this to run it
   SETUP.md                  this file
```

**Steps**

1. Put the folder anywhere, e.g. `C:\QuickBooks Automation`.
2. Open your company in QuickBooks Desktop.
3. If there is no `config.json`, copy `config.example.json` to `config.json`.
   Nothing to edit.
4. Double-click **Start Dashboard.cmd**. A black window opens and your browser
   goes to the dashboard.
5. On the **Status** page, pick your company and paste the sheet link — see
   *Point it at your company and your sheet* in step 5 above.

**You must be watching QuickBooks the first time.** QuickBooks will pop up a
permission window asking whether to let "QB Automation Dashboard" read and
write. Choose **"Yes, whenever this company file is open"** and Continue.

> **If you miss that prompt, QuickBooks remembers it as a refusal** and from
> then on every attempt fails with *"The QuickBooks user has denied access."*
> To undo it: in QuickBooks go to **Edit → Preferences → Integrated
> Applications → Company Preferences**, select "QB Automation Dashboard",
> click **Remove**, then start the dashboard again and approve the prompt.

**Windows may warn about an unrecognised app.** The .exe is not code-signed, so
SmartScreen may say "Windows protected your PC". Click **More info → Run
anyway**. Some antivirus tools are also suspicious of newly built .exe files;
if yours quarantines it, allow it.

To stop the dashboard, close the black window.
