# TubeNotes.ai ko existing TrialGuard PostgreSQL ke saath deploy karna

Is setup mein dono domains live rahenge:

| Domain | Folder | API port | Database |
| --- | --- | --- | --- |
| `tubenotes.trueworks.in` | `/opt/trialguard` | `127.0.0.1:8001` | existing PostgreSQL |
| `tubenotes.ai` | `/opt/tubenotes-ai` | `127.0.0.1:8011` | wahi existing PostgreSQL |

`docker-compose.shared-db.prod.yml` mein database service jaan-boojhkar nahi hai.
Isliye `tubenotes.ai` deploy se doosra Postgres, volume, ya port `5434` nahi banega.
Naya API existing `trialguard_default` private Docker network se
`trialguard-db-1:5432` tak directly connect karta hai; host ke `127.0.0.1:5433`
port ko container ke andar use nahi karta.

## 1. Naya code aur environment

DGX par:

```bash
cd /opt
git clone https://github.com/saurabhsachan504/tubenotes-ai.git tubenotes-ai
cd /opt/tubenotes-ai
cp .env.prod.example .env
chmod 600 .env
```

Nayi `.env` mein purani `/opt/trialguard/.env` se **exact same** values copy karo:

- `POSTGRES_PASSWORD`
- `SECRET_KEY`
- `DEVICE_HASH_SECRET`
- `ADMIN_API_KEY`, Google, SMTP, vLLM aur payment provider secrets

In values ko alag mat banana. Same database aur existing users/sessions/trial
ledger ke liye ye secrets same rehne chahiye.

Sirf domain-specific values ye rahengi:

```ini
APP_BASE_URL=https://tubenotes.ai
BILLING_SUCCESS_URL=https://tubenotes.ai/billing/success
BILLING_CANCEL_URL=https://tubenotes.ai/billing/cancel
BILLING_PRIMARY_SITE_URL=
TRIALGUARD_DOCKER_NETWORK=trialguard_default
SHARED_DB_HOST=trialguard-db-1
```

Purani `/opt/trialguard/.env` ko abhi change mat karo. Agar purane domain par
Subscribe button ko TubeNotes.ai par bhejna hai, **same payment-redirect code
is commit se `/opt/trialguard` mein bhi deploy** karne ke baad uski `.env` mein
ye set karo:

```ini
BILLING_PRIMARY_SITE_URL=https://tubenotes.ai
```

## 2. Backup (mandatory)

Naya app startup par Alembic migrations check karta hai. Deployment se pehle
existing database ka backup lo:

```bash
docker exec trialguard-db-1 pg_dump -U trialguard -d trialguard > /opt/trialguard-backup-$(date +%F-%H%M).sql
```

Backup file ka size `ls -lh` se verify karo.

## 3. Naya API start karo

```bash
cd /opt/tubenotes-ai
CONFIRM_SHARED_DB_BACKUP=yes bash deploy/deploy-shared-db.sh
```

Successful output mein `/healthz` ka JSON aayega. Check:

```bash
curl http://127.0.0.1:8001/healthz  # existing site
curl http://127.0.0.1:8011/healthz  # new TubeNotes.ai site
```

## 4. aaPanel/Nginx and Cloudflare

aaPanel mein `tubenotes.ai` ke liye **new site** banao, SSL enable karo, aur
[`deploy/nginx-tubenotes-ai.conf`](deploy/nginx-tubenotes-ai.conf) ka `location /`
block us new site's config mein add karo. Existing trueworks Nginx config ko
replace mat karo.

Cloudflare Tunnel mein `tubenotes.ai` ka new public hostname add karo. Agar
existing trueworks hostname aaPanel/Nginx ke `http://localhost:80` service par
point karta hai, AI hostname bhi exactly usi service par point karega; Nginx
Host header se `tubenotes.ai` ko port `8011` tak bhej dega.

## 5. Payments and Google

- Razorpay webhook URL, success URL aur cancel URL ko `tubenotes.ai` par set
  karo. Existing webhook secret aur plan IDs `.env` mein same rakho.
- Google OAuth console mein **dono** authorized origins (`https://tubenotes.ai`
  aur `https://tubenotes.trueworks.in`) aur their required redirect URLs rakho.
- Sirf new payments TubeNotes.ai se start honge when the legacy deployment is
  updated with `BILLING_PRIMARY_SITE_URL=https://tubenotes.ai`. Us setting ke
  baad legacy Subscribe button redirect karta hai aur legacy checkout API direct
  call par bhi payment session create nahi karti.

## Update later

```bash
cd /opt/tubenotes-ai
git pull --ff-only
CONFIRM_SHARED_DB_BACKUP=yes bash deploy/deploy-shared-db.sh
```
