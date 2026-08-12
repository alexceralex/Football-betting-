# Betano Live Scanner

## Deploy
1. Urcă fișierele într-un repository GitHub.
2. Deschide Streamlit Community Cloud și creează app din repo.
3. Entrypoint: `app.py`.
4. Advanced settings → Secrets:
   `ODDS_API_KEY = "CHEIA_TA"`
5. Deploy.

Nu urca niciodată `.streamlit/secrets.toml` în GitHub.

V1: meciuri live + cote Betano + filtru cotă minimă (implicit 1.40).
După validarea feedului real, V2 poate adăuga model de probabilitate, fair odds, edge și alerte.
