# GPGD Sample Certification Data

Sample input files for **MyGPGD J360** (Agent 1 — Certification Collector), themed to the
Ministry of Education / GPGD context from the *MoE Knowledge Brain* deck. In that deck,
MyGPGD J360 is one of the systems whose weekly exports feed the Knowledge Brain — these
files play the role of those provider certification exports.

## Files

| File | Provider (pick this on upload) | Rows |
|------|-------------------------------|------|
| `GPGD_Google_Certifications.xlsx`    | **Google**    | 6 |
| `GPGD_Microsoft_Certifications.xlsx` | **Microsoft** | 7 |
| `GPGD_Apple_Certifications.xlsx`     | **Apple**     | 5 |

Each file's columns match the exact schema the uploader expects for that provider
(`agents/collector.py → SCHEMA`). Do **not** rename the column headers.

## What the data is designed to demonstrate

Uploading all three and running the pipeline produces:

- **18 total records → 15 unique teachers** (deduplicated by IC number)
- **3 multi-certified** teachers — same person certified by two providers:
  - *Nurul Aisyah binti Ahmad* — Google (GCE L2) + Microsoft (MIEE)
  - *Siti Khadijah binti Osman* — Google (Coach) + Apple (Learning Coach)
  - *Mohd Hafiz bin Abdullah* — Google (Trainer) + Microsoft (MCE)
- **12 eligible / 3 not eligible**. The 3 not-eligible cases each show a different reason:
  - *Ahmad Zaki* — Google Certified Educator **Level 1** (only L2+ qualifies)
  - *Kavitha a/p Ramasamy* — **Microsoft Office Specialist** (not an educator cert)
  - *Suresh a/l Maniam* — **Apple Professional Learning** (not Teacher / Learning Coach)
- **2 deliberately missing cells** to exercise the red-highlight validator:
  - Google row 7 — *Farah Nadia* has **no email** (she's eligible, but the invitation step will skip her — a realistic data-quality case)
  - Microsoft row 8 — *Halimah binti Salleh* has **no Cert Year** (stored as 0)

Teachers span 5 states (Selangor, Johor, Pulau Pinang, Sarawak, Sabah, Kedah), giving the
State Officer review and district/technology quota ranking something to work with.

## How to use (demo flow)

1. Log in as an officer at `/agents/login/`.
2. **Upload** each file at `/agents/upload-certifications/`, choosing the matching provider.
   (Missing-value files upload with a warning, not a rejection — that's intended.)
3. The autonomous pipeline agent runs automatically after each upload *if* `ANTHROPIC_API_KEY`
   is set; otherwise use **Process All Files** on the dashboard to run the pipeline manually.
4. Check the **Dashboard** for the unique/eligible/multi-certified counts above.
5. **Send Invitations** → eligible teachers with an email get a secure application link.
6. Open an invite link, **submit an application**, then **Compile & Rank** and walk the
   State Officer → MoE Officer approval gates.

> Prerequisite: provider eligibility rules must be seeded. If eligibility comes back all
> "Not Eligible", run `python manage.py seed_providers` once, then re-process.

_Generated as sample/test data — all names, IC numbers, emails, and schools are fictitious._
