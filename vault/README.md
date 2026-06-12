# Cenas Vault Cloud

Cloud mirror of C:\Cenas, served by cenas_vault_cloud.py on Render.

## Setup (two manual steps)

### Step 1: Create the service from the Blueprint

1. Render dashboard -> New -> Blueprint.
2. Connect the repo samsahragard/cenas-ezlive.
3. Render finds vault/render.yaml automatically.
4. Click Apply / Create to create the cenas-vault service.

### Step 2: Set VAULT_TOKEN

In the new service: Environment tab -> add the value for VAULT_TOKEN
(the Blueprint creates the key but leaves the value blank on purpose;
it is never stored in the repo).

Generate a long random token in PowerShell:

    -join ([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32) | ForEach-Object ToString x2)

Paste the output as the VAULT_TOKEN value and keep a copy somewhere safe
(you need the same token for the local sync worker and for browser login).

## Opening the vault

Browse to https://cenas-vault.onrender.com

Basic auth login: user "sam", password = the VAULT_TOKEN value.

Note: no health check path is set in the Blueprint because the site
returns 401 without credentials; Render falls back to its default
port check, which is fine.

## What the sync worker does

The local worker mirrors C:\Cenas to the cloud every 5 minutes. Junction
points, blocklisted folders, and 08 Archive are excluded from the sync.
Files that appear first in the cloud are landed locally under
99 Inbox\from-cloud so nothing new ever overwrites your tree silently.
Deletes only propagate via tombstones (an explicit delete record), never
by simple absence, and any file that would be displaced by a sync is
quarantined on the disk instead of destroyed. Every node keeps an
undo/audit trail in its own sqlite log, so any change can be traced and
rolled back.
