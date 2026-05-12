#!/usr/bin/env bash
# Generate a Cenas Kitchen Employee Android upload keystore.
#
# Run this ONCE on a trusted machine. Keep the resulting upload-keystore.jks
# in a safe place (1Password, encrypted USB, the AiCk secrets folder).
# Copy the base64 contents into the ANDROID_UPLOAD_KEYSTORE_B64 GitHub Secret;
# put the passwords in the matching ANDROID_KEYSTORE_PASSWORD / ANDROID_KEY_*
# secrets. After that CI will produce signed AABs on every push to mobile/.
#
# We use Play App Signing: this keystore is the "upload key", not the
# production signing key. Google Play re-signs the AAB with their own
# production key for distribution. That means you can rotate THIS key in
# Play Console if it's ever compromised — but you cannot lose this key
# without filing a key-reset request with Google (slow).
#
# Requires: a Java JDK (`keytool` on $PATH). Comes with Android Studio or any
# JDK 11+ install.
set -euo pipefail

OUT="${1:-upload-keystore.jks}"

if [[ -e "$OUT" ]]; then
  echo "ERROR: $OUT already exists. Move it out of the way or rerun with a different path." >&2
  exit 2
fi

read -rp "Keystore password (used for the whole .jks file): " -s KEYSTORE_PASSWORD
echo
read -rp "Confirm keystore password:                       " -s KEYSTORE_PASSWORD2
echo
if [[ "$KEYSTORE_PASSWORD" != "$KEYSTORE_PASSWORD2" ]]; then
  echo "ERROR: keystore passwords don't match" >&2
  exit 2
fi
read -rp "Key alias (suggest 'upload'):                     " KEY_ALIAS
KEY_ALIAS="${KEY_ALIAS:-upload}"
read -rp "Key password (can match keystore password):       " -s KEY_PASSWORD
echo

keytool -genkeypair \
  -v \
  -keystore "$OUT" \
  -alias "$KEY_ALIAS" \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000 \
  -storepass "$KEYSTORE_PASSWORD" \
  -keypass "$KEY_PASSWORD" \
  -dname "CN=Cenas Kitchen, OU=Engineering, O=Cenas Kitchen LLC, L=Tomball, ST=TX, C=US"

echo
echo "Done. Keystore: $OUT"
echo
echo "Next:"
echo "  1. Base64-encode for GitHub Secrets:"
echo "       base64 -w0 \"$OUT\" | clip       # Windows (Git Bash)"
echo "       base64 -w0 \"$OUT\" | pbcopy     # macOS"
echo "  2. Add four GitHub Secrets to leverr18/cenas-ezlive (Settings → Secrets → Actions):"
echo "       ANDROID_UPLOAD_KEYSTORE_B64  = (the base64 from step 1)"
echo "       ANDROID_KEYSTORE_PASSWORD    = (the password you just entered)"
echo "       ANDROID_KEY_ALIAS            = $KEY_ALIAS"
echo "       ANDROID_KEY_PASSWORD         = (the key password you entered)"
echo "  3. Trigger the workflow (push to mobile/** or run mobile-android.yml manually)."
echo "     The release-aab job will produce a signed cenas-kitchen-employee.aab artifact."
echo "  4. Download the AAB from the workflow run and upload to Play Console → Internal Testing."
echo
echo "BACKUP THIS FILE somewhere safe. If you lose it, you need Google's"
echo "key-reset process (1-2 weeks) to upload further releases."
