# Soniq — YT Audio Extractor

CI/CD-enabled deployment to Google Cloud Run.  
Works with **GitHub Actions** and **Codeberg / Gitea Actions**.

---

## Repository structure

```
soniq/
├── .github/
│   └── workflows/
│       └── deploy.yml        # GitHub Actions pipeline
├── .gitea/
│   └── workflows/
│       └── deploy.yml        # Codeberg / Gitea Actions pipeline
├── tests/
│   └── test_server.py        # pytest unit tests
├── static/
│   └── index.html            # Frontend UI
├── server.py                 # Flask backend
├── Dockerfile                # Multi-stage container build
├── requirements.txt          # Python dependencies
├── pytest.ini                # Test config
├── .gitignore
└── README.md
```

---

## CI/CD pipeline

Every push and pull request runs **lint + unit tests**.  
Pushes to `master` additionally **build the Docker image**, push it to Container Registry, deploy to Cloud Run, and run a live health check.

```
push to master
   │
   ├─► lint (flake8) + unit tests (pytest)
   │         │ pass
   │         ▼
   │   docker build → push to GCR
   │         │
   │         ▼
   │   gcloud run deploy
   │         │
   │         ▼
   │   smoke test  GET /healthz → 200
   │
pull request → tests only, no deploy
```

---

## One-time GCP setup

Run these commands once before your first push.

### 1. Enable APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com
```

### 2. Create a Service Account

```bash
export PROJECT_ID=$(gcloud config get-value project)

gcloud iam service-accounts create soniq-cicd \
  --display-name "YT Audio CI/CD deployer"

# Grant required roles
for ROLE in \
  roles/run.admin \
  roles/storage.admin \
  roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:soniq-cicd@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role "$ROLE"
done
```

---

## GitHub Actions setup

### Option A — Workload Identity Federation (recommended, no long-lived keys)

```bash
# Create the WIF pool
gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions pool"

# Get the pool resource name
POOL=$(gcloud iam workload-identity-pools describe "github-pool" \
  --location="global" \
  --format="value(name)")

# Create a provider for GitHub
gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Bind your repo to the service account
# Replace YOUR_GITHUB_USERNAME/YOUR_REPO_NAME
gcloud iam service-accounts add-iam-policy-binding \
  "soniq-cicd@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL}/attribute.repository/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME"

# Get provider resource name (add this to GitHub secrets as GCP_WIF_PROVIDER)
gcloud iam workload-identity-pools providers describe "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)"
```

**GitHub secrets to add** (repo Settings → Secrets → Actions):

| Secret name | Value |
|---|---|
| `GCP_PROJECT_ID` | your GCP project ID |
| `GCP_REGION` | e.g. `us-central1` |
| `GCP_WIF_PROVIDER` | WIF provider resource name (from above) |
| `GCP_SERVICE_ACCOUNT` | `soniq-cicd@YOUR_PROJECT.iam.gserviceaccount.com` |

### Option B — Service Account JSON key (simpler)

```bash
gcloud iam service-accounts keys create sa-key.json \
  --iam-account "soniq-cicd@${PROJECT_ID}.iam.gserviceaccount.com"
```

Add the contents of `sa-key.json` as secret `GCP_SA_KEY`.  
In `.github/workflows/deploy.yml`, comment out the WIF auth block and uncomment the SA key block.

**Delete `sa-key.json` from your machine afterwards** — never commit it.

---

## Codeberg / Gitea Actions setup

Gitea Actions is API-compatible with GitHub Actions and uses the same workflow syntax with minor differences (`gitea.sha` instead of `github.sha`, etc.).

### 1. Install and register an act_runner

```bash
# On your runner machine (a VPS, Raspberry Pi, etc.)
# Download act_runner from: https://gitea.com/gitea/act_runner/releases

# Register the runner to your Codeberg/Gitea repo
./act_runner register \
  --instance https://codeberg.org \
  --token YOUR_RUNNER_TOKEN \
  --name my-runner \
  --labels ubuntu-latest:docker://node:20-bookworm

# Start the runner
./act_runner daemon
```

Get the runner token from your repo: **Settings → Actions → Runners → Create new runner**.

### 2. Add secrets to Codeberg

Repo **Settings → Secrets → Add secret**:

| Secret name | Value |
|---|---|
| `GCP_PROJECT_ID` | your GCP project ID |
| `GCP_REGION` | e.g. `us-central1` |
| `GCP_SA_KEY` | contents of `sa-key.json` |

> Codeberg's Gitea Actions does not yet support Workload Identity Federation, so use Option B (SA key) for Codeberg.

---

## Running tests locally

```bash
pip install -r requirements.txt
pip install pytest pytest-cov flake8

# Lint
flake8 server.py --max-line-length=120

# Tests
pytest tests/ -v --cov=server
```

---

## Secrets summary

| Secret | Used by | Description |
|---|---|---|
| `GCP_PROJECT_ID` | both | GCP project ID |
| `GCP_REGION` | both | Cloud Run region |
| `GCP_WIF_PROVIDER` | GitHub only | WIF provider resource name |
| `GCP_SERVICE_ACCOUNT` | GitHub only | SA email for WIF |
| `GCP_SA_KEY` | Codeberg / Option B | SA key JSON (base64 not needed) |

---

## Local development

```bash
pip install -r requirements.txt   # needs ffmpeg in PATH
python server.py                  # http://localhost:5000
```

---

## Making a release

Just push to `master` — the pipeline handles everything:
1. Tests pass
2. Docker image built with the commit SHA as tag
3. Deployed to Cloud Run
4. Health check confirms it's live
