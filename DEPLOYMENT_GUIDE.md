# Production Deployment Guide
**AccelDocs Backend** | FastAPI + PostgreSQL + Google Drive

---

## Prerequisites

- Python 3.11+ installed
- Docker (optional, for containerized deployment)
- PostgreSQL database (production)
- Google Cloud Project with Drive API enabled
- Google OAuth credentials configured
- Domain name or hosting platform account

---

## Step 1: Environment Configuration

Create `.env.production`:

```bash
# Database (PostgreSQL for production)
DATABASE_URL=postgresql://user:password@host:5432/acceldocs_prod

# Google OAuth
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_OAUTH_TOKEN_FILE=/app/oauth-token.json

# Google Drive Service Account (if using)
GOOGLE_SERVICE_ACCOUNT_FILE=/app/service-account.json
GOOGLE_DRIVE_ROOT_FOLDER_ID=1234567890abcdefg

# Zensical Docs Repository
DOCS_REPO_PATH=/app/docs-site
DOCS_REPO_URL=https://github.com/yourusername/docs-site.git

# Server Configuration
HOST=0.0.0.0
PORT=8000
SECRET_KEY=CHANGE-THIS-TO-A-STRONG-RANDOM-SECRET-KEY-IN-PRODUCTION

# CORS Origins (your frontend URLs)
ALLOWED_ORIGINS=https://acceldocs.vercel.app,https://docs.yourcompany.com

# AI Documentation Agent (required for agent chat and template generation)
ANTHROPIC_API_KEY=sk-ant-...

# Netlify (for docs site deployment)
NETLIFY_SITE_ID=your-netlify-site-id
NETLIFY_AUTH_TOKEN=your-netlify-auth-token
```

### Generate Strong Secret Key

```bash
# Use Python
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# Or use OpenSSL
openssl rand -base64 64
```

---

## Step 2: Database Setup

### Option A: PostgreSQL on Railway

1. Create account at [railway.app](https://railway.app)
2. Create new project → Add PostgreSQL
3. Copy connection string to `DATABASE_URL`

### Option B: PostgreSQL on Render

1. Create account at [render.com](https://render.com)
2. New → PostgreSQL
3. Copy Internal Database URL to `DATABASE_URL`

### Option C: Supabase PostgreSQL

1. Create project at [supabase.com](https://supabase.com)
2. Project Settings → Database → Connection string
3. Use the Postgres connection string (not Supabase URL)

### Run Migrations

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment
export DATABASE_URL="your-postgres-url"

# Run migrations
alembic upgrade head
```

### Seed Initial Admin User

```bash
python3 -c "
from app.database import get_db
from app.models import User

db = next(get_db())
admin = User(
    google_id='admin-setup',
    email='admin@yourcompany.com',
    name='Admin User',
    role='admin'
)
db.add(admin)
db.commit()
print('Admin user created')
"
```

---

## Step 3: Deployment Options

### Option A: Railway (Recommended)

**Pros:** Easy setup, auto-deploy from Git, built-in PostgreSQL, good free tier

1. **Connect Repository**
   ```
   - Go to railway.app
   - New Project → Deploy from GitHub repo
   - Select acceldocs-backend repository
   ```

2. **Configure Environment**
   ```
   - Settings → Variables → Bulk Import
   - Paste contents of .env.production
   ```

3. **Set Build Command**
   ```
   Settings → Deploy:
   - Build Command: pip install -r requirements.txt
   - Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```

4. **Deploy**
   ```
   - Railway auto-deploys on push to main branch
   - Get deployment URL from Settings → Domains
   ```

### Option B: Render

**Pros:** Free SSL, auto-deploy, similar to Heroku

1. **Create Web Service**
   ```
   - Dashboard → New → Web Service
   - Connect GitHub repository
   ```

2. **Configure**
   ```
   Name: acceldocs-backend
   Environment: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```

3. **Add Environment Variables**
   ```
   - Environment → Add from .env
   - Paste all variables
   ```

### Option C: Fly.io

**Pros:** Multi-region, edge deployment, Docker-based

1. **Install Fly CLI**
   ```bash
   curl -L https://fly.io/install.sh | sh
   ```

2. **Create Dockerfile**
   ```dockerfile
   FROM python:3.11-slim

   WORKDIR /app

   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt

   COPY . .

   CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
   ```

3. **Deploy**
   ```bash
   fly launch
   fly secrets set SECRET_KEY="your-secret-key"
   fly secrets set DATABASE_URL="your-postgres-url"
   # ... set all other secrets
   fly deploy
   ```

### Option D: DigitalOcean App Platform

1. **Create App**
   ```
   - Apps → Create App → GitHub repository
   - Select acceldocs-backend
   ```

2. **Configure**
   ```
   Type: Python
   HTTP Port: 8000
   Run Command: uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

3. **Add Environment Variables**
   ```
   - Settings → App-Level Environment Variables
   - Add all from .env.production
   ```

### Option E: Traditional VPS (DigitalOcean Droplet, Linode)

1. **SSH into server**
   ```bash
   ssh root@your-server-ip
   ```

2. **Install dependencies**
   ```bash
   apt update && apt upgrade -y
   apt install python3.11 python3-pip python3-venv nginx supervisor git -y
   ```

3. **Clone repository**
   ```bash
   cd /var/www
   git clone https://github.com/yourusername/acceldocs-backend.git
   cd acceldocs-backend
   ```

4. **Set up Python environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. **Create systemd service**
   ```bash
   nano /etc/systemd/system/acceldocs.service
   ```

   ```ini
   [Unit]
   Description=AccelDocs Backend
   After=network.target

   [Service]
   User=www-data
   WorkingDirectory=/var/www/acceldocs-backend
   Environment="PATH=/var/www/acceldocs-backend/.venv/bin"
   EnvironmentFile=/var/www/acceldocs-backend/.env.production
   ExecStart=/var/www/acceldocs-backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

6. **Configure Nginx**
   ```bash
   nano /etc/nginx/sites-available/acceldocs
   ```

   ```nginx
   server {
       listen 80;
       server_name api.yourcompany.com;

       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

   ```bash
   ln -s /etc/nginx/sites-available/acceldocs /etc/nginx/sites-enabled/
   nginx -t
   systemctl restart nginx
   ```

7. **Enable SSL with Let's Encrypt**
   ```bash
   apt install certbot python3-certbot-nginx -y
   certbot --nginx -d api.yourcompany.com
   ```

8. **Start services**
   ```bash
   systemctl enable acceldocs
   systemctl start acceldocs
   systemctl status acceldocs
   ```

---

## Step 4: Post-Deployment Configuration

### Set Up Google OAuth Callback

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. APIs & Services → Credentials
3. Edit OAuth 2.0 Client ID
4. Add authorized redirect URIs:
   ```
   https://your-backend-url.com/auth/callback
   https://your-frontend-url.com/auth/callback
   ```

### Configure CORS Origins

Update `ALLOWED_ORIGINS` in production environment:
```
ALLOWED_ORIGINS=https://your-frontend.netlify.app,https://docs.yourcompany.com,https://admin.yourcompany.com
```

### Test Deployment

```bash
# Health check
curl https://your-backend-url.com/health

# Expected response:
# {"status":"ok","service":"acceldocs-backend"}
```

---

## Step 5: Monitoring & Logging

### Add Sentry for Error Tracking

1. Create account at [sentry.io](https://sentry.io)
2. Create new project (Python/FastAPI)
3. Add to `requirements.txt`:
   ```
   sentry-sdk[fastapi]>=1.40.0
   ```

4. Update `app/main.py`:
   ```python
   import sentry_sdk

   sentry_sdk.init(
       dsn="your-sentry-dsn",
       traces_sample_rate=1.0,
       environment="production",
   )
   ```

### Set Up Uptime Monitoring

- **UptimeRobot:** Free, monitors /health endpoint
- **Pingdom:** More features, analytics
- **Better Uptime:** Modern UI, status pages

Configure to ping `https://your-backend-url.com/health` every 5 minutes.

### Enable Application Logging

Update `app/main.py`:
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/var/log/acceldocs/app.log"),
        logging.StreamHandler()
    ]
)
```

---

## Step 6: CI/CD Pipeline

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy to Production

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: |
          pip install -r requirements.txt
          pytest tests/test_e2e_*.py

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Deploy to Railway
        run: |
          curl https://railway.app/deploy \
            -H "Authorization: Bearer ${{ secrets.RAILWAY_TOKEN }}"
```

---

## Step 7: Database Backups

### Railway Auto-Backups
Railway PostgreSQL includes automatic daily backups (paid plan).

### Manual Backup Script

```bash
#!/bin/bash
# backup-db.sh

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backups"
DB_URL="your-postgres-url"

pg_dump $DB_URL > $BACKUP_DIR/acceldocs_$DATE.sql

# Keep only last 30 days
find $BACKUP_DIR -name "acceldocs_*.sql" -mtime +30 -delete

# Upload to S3 (optional)
aws s3 cp $BACKUP_DIR/acceldocs_$DATE.sql s3://your-bucket/backups/
```

Add to cron:
```bash
crontab -e
# Run daily at 2 AM
0 2 * * * /path/to/backup-db.sh
```

---

## Step 8: Production Optimization

### Enable Database Connection Pooling

Update `app/database.py`:
```python
engine = create_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
)
```

### Add Rate Limiting

```bash
pip install slowapi
```

Update `app/main.py`:
```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Apply to endpoints
@router.post("/api/analytics/track/view/{doc_id}")
@limiter.limit("100/minute")
async def track_view(...):
    ...
```

### Enable Gzip Compression

```python
from fastapi.middleware.gzip import GZipMiddleware

app.add_middleware(GZipMiddleware, minimum_size=1000)
```

---

## Troubleshooting

### Database Connection Errors

```bash
# Check connection
psql $DATABASE_URL -c "SELECT version();"

# Test from Python
python3 -c "from app.database import engine; print(engine.connect())"
```

### Migration Issues

```bash
# Check current revision
alembic current

# Show history
alembic history

# Downgrade if needed
alembic downgrade -1

# Re-upgrade
alembic upgrade head
```

### Google OAuth Errors

- Verify redirect URIs in Google Console match exactly
- Check `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
- Ensure OAuth consent screen is configured

### CORS Errors

- Check `ALLOWED_ORIGINS` includes frontend URL
- Verify protocol (http vs https)
- Ensure no trailing slashes in origins

---

## Security Checklist

- [ ] Strong `SECRET_KEY` generated and set
- [ ] Database password is complex and unique
- [ ] Google OAuth credentials secured (not in git)
- [ ] HTTPS/TLS enabled (Let's Encrypt or platform SSL)
- [ ] CORS limited to specific origins (not `*`)
- [ ] Rate limiting enabled on public endpoints
- [ ] Database backups configured
- [ ] Error logging to Sentry (no sensitive data in logs)
- [ ] Environment variables not committed to git
- [ ] Admin user password changed from default
- [ ] Firewall rules configured (if VPS)

---

## Production URL Structure

After deployment, your URLs should look like:

- **Backend API:** `https://api.yourcompany.com`
- **Health Check:** `https://api.yourcompany.com/health`
- **Auth Callback:** `https://api.yourcompany.com/auth/callback`
- **Analytics Dashboard:** `https://api.yourcompany.com/analytics`
- **Docs:** `https://api.yourcompany.com/docs` (FastAPI auto-docs)

---

## Next Steps After Deployment

1. ✅ **Verify health endpoint responds**
2. ✅ **Test Google OAuth login**
3. ✅ **Sync first document from Google Drive**
4. ✅ **Verify analytics tracking**
5. ✅ **Test approval workflow**
6. ✅ **Set up monitoring alerts**
7. ✅ **Configure database backups**
8. ✅ **Add custom domain (if applicable)**

---

## Support & Resources

- **FastAPI Docs:** https://fastapi.tiangolo.com
- **SQLAlchemy:** https://docs.sqlalchemy.org
- **Alembic Migrations:** https://alembic.sqlalchemy.org
- **Railway Docs:** https://docs.railway.app
- **Render Docs:** https://render.com/docs

---

**Your system is now production-ready! 🚀**

For questions or issues, refer to the E2E_TEST_REPORT.md for verified functionality.
