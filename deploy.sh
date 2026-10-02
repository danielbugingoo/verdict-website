#!/bin/bash
set -e
MESSAGE="${1:-deploy}"
git add .
git commit -m "$MESSAGE" || echo "Nothing new to commit"
git push origin main
ssh emiliofuerte@ssh.pythonanywhere.com "cd ~/verdict-website && cp newsletter_site/db.sqlite3 newsletter_site/db.sqlite3.pre-deploy-backup && git pull && venv/bin/pip install -q -r requirements.txt && venv/bin/python newsletter_site/manage.py migrate --noinput && venv/bin/python newsletter_site/manage.py collectstatic --noinput && touch newsletter_site/newsletter_site/wsgi.py && touch /var/www/www_columbiaverdict_org_wsgi.py"
echo "Done! Site is live."
