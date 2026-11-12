# BillGen

BillGen is a Flask web app with two bill generators behind one login: **UP Electricity Bill (PDF)** for Uttar Pradesh DISCOM-style bills, and **Bulk Bills (Excel)**, a slab-based generator that turns a usage sheet and a tariff workbook into `bills.xlsx`. Users get 5 free actions per day (IST) and can pay Rs 499 per month via Razorpay for unlimited use.

## Requirements
- Python 3.10+
- A Razorpay account (test mode is enough to start)

## Setup
**Windows**
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
**macOS / Linux**
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
Edit `.env` and fill in the values (`ADMIN_EMAILS` lists users who can open `/admin/tariff`).

## Run
```
python app.py
```
Open http://127.0.0.1:5000. Run the tests with `python -m pytest`.

## What counts as an action
- One PDF download = 1 action. One Excel generate = 1 action.
- Preview, history re-downloads and any request that fails validation are free.
- The action is consumed only after the bill has been calculated successfully.
- The free counter resets at midnight IST. Paid users are never blocked or counted.

## Testing Razorpay in test mode
1. Use `rzp_test_` keys in `.env`.
2. Pay with a test card from the Razorpay docs (test card numbers are listed there).
3. For the webhook: run `ngrok http 5000`, then in the Razorpay dashboard (Settings > Webhooks) add `https://<ngrok-url>/webhook/razorpay`, select `payment.captured` and `order.paid`, and set the same secret as `RAZORPAY_WEBHOOK_SECRET`.

## UP electricity bill: tariffs
On first start the database is seeded with UP tariff categories read from a photo of the tariff sheet. **Verify every rate against the official tariff order before billing.** Rows that were unreadable in the photo (some rural, tubewell, temporary and EV rows) were not invented; admins can add them at `/admin/tariff`. FL charge defaults to 0 (the sample bill used 0.48%); you can override FL %, ED %, energy rate and fixed rate per bill.

Calculation order: kWh/kVAh differences, billed units (gross minus solar export), energy charge (slabs apply per month), fixed charge (months x load x rate), penalty demand and MCG, FL charge, electricity duty, then arrear, LPSC and security adjustment. EC, FC, FL and ED are each rounded to a whole rupee using round-half-down.

Worked example (also the acceptance test): LMV-2, KVAH basis, 19 months, KVAH 2210 to 23066 (20856), solar export 9580, billed units 11276, rate Rs 6.75, fixed Rs 405 on 11.11 KVA, FL 0.48%, ED 7.5%.
EC 76,113 | FC 85,491 | subtotal 1,61,604 | FL 776 | ED 12,178 | total 1,74,558 | paid 1,78,060 | credit 3,502.

## Bulk Excel: input formats
`tariff.xlsx`, sheet **Slabs** (leave `To` blank for no upper limit):

| From | To  | Rate |
|------|-----|------|
| 0    | 100 | 5.50 |
| 100  | 300 | 6.50 |
| 300  |     | 7.50 |

Sheet **Charges** (types: `fixed`, `percent_of_energy`, `percent_of_subtotal`):

| Name           | Type                | Value |
|----------------|---------------------|-------|
| Fixed Charge   | fixed               | 110   |
| Fuel Surcharge | percent_of_energy   | 5     |
| GST            | percent_of_subtotal | 18    |

`usage.xlsx` (first sheet):

| Customer_ID | Name | Units |
|-------------|------|-------|
| C001        | Amit | 250   |
| C002        | Ravi | 80    |

Every `percent_of_subtotal` row is calculated on the same subtotal (not compounded). Output has two sheets: **Summary** and **Bill_Details**.

## Database
SQLite by default. Set `DATABASE_URL` for PostgreSQL (a `postgres://` prefix is rewritten automatically; add `psycopg2-binary` to requirements).

## Deployment (Render or Railway)
- Start command: `gunicorn app:app`
- Set all variables from `.env.example` in the host's dashboard, including a long random `SECRET_KEY`.
- Use a PostgreSQL database (SQLite files are wiped on most hosts).
- Add your public webhook URL in Razorpay. Switch to live keys only after testing.

## Known MVP limits
- No CSRF protection (add Flask-WTF `CSRFProtect` before production).
- Login limiter is in memory (resets on restart); use Redis in production.
- No email verification or password reset.
- Seeded tariffs must be verified against the official order.
- Editing a category in the admin page only affects new bills; saved history keeps its tariff snapshot.
