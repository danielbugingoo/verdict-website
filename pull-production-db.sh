#!/bin/bash
set -e
cd "$(dirname "$0")"
cp newsletter_site/db.sqlite3 newsletter_site/db.sqlite3.local-backup
echo "Backed up local db.sqlite3 to newsletter_site/db.sqlite3.local-backup"
scp emiliofuerte@ssh.pythonanywhere.com:~/verdict-website/newsletter_site/db.sqlite3 newsletter_site/db.sqlite3
echo "Pulled production data into local db.sqlite3"
echo "Run venv/bin/python newsletter_site/manage.py migrate now if local has migrations production doesn't."
