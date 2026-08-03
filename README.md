# ZorgVerhaal AI — Railway demo v2

Mobiele rapportage-assistent voor kleinschalige Nederlandse gehandicapten- en ouderenzorg. De app gebruikt echte OpenAI-aanroepen om per beurt één contextuele vervolgvraag te kiezen. Er is geen vaste vragenlijst: het model beoordeelt bronverhaal, eerdere antwoorden, cliëntcontext, actieve zorgdoelen en juridische/veiligheidskaders opnieuw.

## Drie dashboards

- **Platformadmin:** alleen de eigenaar; maakt zorgaanbieders aan en ontvangt hun documentuploads in een verwerkings-inbox.
- **Organisatiebeheerder:** vult de fictieve organisatie, cliënten, zorgdoelen en reminders; uploadt eigen formulieren; nodigt medewerkers uit en wijst cliënten toe.
- **Medewerker:** ziet alleen toegewezen cliënten, reminders, zorgdoelen en de mobiele dagelijkse rapportageflow.

Een organisatiebeheerder kan zonder apart account de medewerkersdemo openen. Alle schermen dragen een duidelijke fictieve-demo-waarschuwing.

## Wat deze versie doet

- Vrij verhaal typen of via browser-spraakherkenning dicteren.
- Dynamisch doorvragen totdat de AI voldoende relevante informatie ziet.
- Acute risico's vóór administratieve volledigheid behandelen.
- Mogelijke signalen tonen voor Wkkgz, Wzd, WGBO, AVG/Wabvpz en meldcode.
- Nooit zelf een melding, diagnose, Wzd-besluit of behandelbesluit uitvoeren.
- Brongetrouw concept met Structured Outputs.
- Menselijke eindcontrole, doelkeuze, tijdregistratie en incidentbevestiging.
- PostgreSQL, organisatie-afscherming, Argon2-wachtwoorden, versleutelde zorginhoud en auditlog.
- `store=False` bij OpenAI-responses.
- Uitnodigingslinks verlopen, zijn intrekbaar en hebben een gebruikslimiet.
- Documentuploads (PDF, DOCX, TXT of JSON) verschijnen als signaal in de privé-admin-inbox.
- Fictieve cliënten kunnen worden gearchiveerd; demo-inhoud kan gecontroleerd worden gereset.

## Basisformulier dagelijkse zorg

Het organisatieformulier wordt als context aan de AI meegegeven, niet als vaste vragenlijst. De demo dekt waar relevant: welzijn, slaap, ADL, eten/drinken, toiletgang, mobiliteit, medicatie-afwijkingen, gedrag, communicatie, activiteiten, cliëntperspectief, begeleiding en effect, zorgdoelen, risico/incident/verzet, overdracht, tijdregistratie en menselijke bevestiging. De AI kiest zelf welke ontbrekende informatie één voor één moet worden uitgevraagd.

Formele verplichte velden worden door applicatieregels afgedwongen. AI-tekst, doelkoppelingen, incidentwaarschuwingen en acties blijven voorstellen totdat een medewerker ze expliciet controleert.

## Railway

1. Maak een nieuw Railway-project vanuit deze repository.
2. Voeg PostgreSQL toe.
3. Stel variabelen in vanuit `.env.example`. Railway levert `DATABASE_URL`; de app zet Railway's PostgreSQL-URL automatisch om voor psycopg.
4. Genereer sleutels:
   - `DATA_ENCRYPTION_KEY`: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `JWT_SECRET`: minimaal 32 willekeurige bytes.
5. Zet `COOKIE_SECURE=true` en `ALLOWED_ORIGINS=https://jouwdomein.nl`.
6. Vul `OPENAI_API_KEY`, `BOOTSTRAP_ADMIN_EMAIL` en een uniek lang `BOOTSTRAP_ADMIN_PASSWORD` in.
7. Deploy. Healthcheck: `/health`.

Gebruik voor een bestaande database later formele Alembic-migraties. Deze demoversie initialiseert een nieuwe Railway-database automatisch en is bedoeld voor een schone eerste installatie.

## Lokaal

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000`.

## Voor echte cliëntgegevens

Dit is een technische MVP, geen kant-en-klare wettelijke certificering. Voor productie zijn minimaal nodig: DPIA, verwerkingsregister, grondslag/doelbinding, verwerkersovereenkomsten en beoordeling doorgifte, passende OpenAI data controls/contractkeuze, NEN 7510/7512/7513-inrichting, formele autorisatiematrix, back-up/herstel, incidentrespons, bewaartermijnen/vernietiging, rechtenafhandeling, pentest, protocollen per aanbieder en inhoudelijke validatie door FG/privacyjurist, kwaliteitsfunctionaris, Wzd-functionaris en zorgprofessionals.

De wettelijke kennis in `app/legal_policy.py` is versiebeheerbare beslisondersteuning. Laat wijzigingen altijd juridisch en professioneel goedkeuren; laat het model nooit live wetten interpreteren of zelfstandig beslissen.
