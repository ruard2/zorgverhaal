# ZorgVerhaal AI — Railway demo v2

Mobiele rapportage-assistent voor kleinschalige Nederlandse gehandicapten- en ouderenzorg. Applicatieregels handelen metadata, diensten, formulierrouting en validatie af; OpenAI wordt alleen gebruikt voor taalbegrip, feitelijke extractie, relevante verheldering en concepttekst. Ontbrekende vragen worden per ronde gebundeld.

## Drie dashboards

- **Platformadmin:** alleen de eigenaar; maakt zorgaanbieders aan en ontvangt hun documentuploads in een verwerkings-inbox.
- **Organisatiebeheerder:** vult de fictieve organisatie, cliënten, zorgdoelen en reminders; uploadt eigen formulieren; nodigt medewerkers uit en wijst cliënten toe.
- **Medewerker:** ziet alleen toegewezen cliënten, reminders, zorgdoelen en de mobiele dagelijkse rapportageflow.

Een organisatiebeheerder kan zonder apart account de medewerkersdemo openen. Alle schermen dragen een duidelijke fictieve-demo-waarschuwing.

## Wat deze versie doet

- Vrij verhaal typen of via browser-spraakherkenning dicteren.
- Maximaal vier gebundelde vragen per ronde, zodat meestal één aanvullende AI-call volstaat.
- GPT-5.6 Terra zonder reasoning voor snelle dagelijkse extractie; Sol wordt alleen gebruikt voor eenmalige formulierimport.
- Vaste registratiegegevens en formulierselectie worden door de applicatie afgehandeld; alleen resterende vrije tekst gaat naar de rapportage-AI.
- De medewerker ziet direct een lokaal concept met bronnotitie en bekende dienstgegevens terwijl de inhoudelijke AI-call loopt.
- De medewerkershomepage groepeert open dagtaken, toegewezen cliënten en alle actieve formulieren. Een bewust gekozen formulier gebruikt dezelfde cliëntkeuze, vrije invoer, AI-verheldering en menselijke eindcontrole, waarbij alleen dat formulier naar de AI gaat.
- Dagrapportages worden stabiel verdeeld over Terra (controle) en Luna (experiment); audittelemetrie maakt vergelijking van latency, tokens en kwaliteit mogelijk.
- De dagelijkse rapportage verstuurt nooit de uitgebreide formulier-specifieke juridische prompt. Een gekozen aanvullend formulier wordt in de huidige flow handmatig ingevuld, dus daarvoor is geen extra AI-call nodig. API-responses worden niet bij OpenAI opgeslagen (`store=false`).
- Relevante incident-, medicatie-, Wzd- en crisisformulieren worden direct in dezelfde call als concept ingevuld.
- Harde API-timeout zonder verborgen automatische retry; invoer blijft bij een fout behouden.
- Auditlog met modelroute, latency, tokengebruik, cachegebruik en OpenAI response-ID.
- Acute risico's vóór administratieve volledigheid behandelen.
- Mogelijke signalen tonen voor Wkkgz, Wzd, WGBO, AVG/Wabvpz en meldcode.
- Nooit zelf een melding, diagnose, Wzd-besluit of behandelbesluit uitvoeren.
- Brongetrouw concept met Structured Outputs.
- Menselijke eindcontrole, doelkeuze, tijdregistratie en incidentbevestiging.
- PostgreSQL, organisatie-afscherming, Argon2-wachtwoorden, versleutelde zorginhoud en auditlog.
- `store=False` bij OpenAI-responses.
- Uitnodigingslinks verlopen, zijn intrekbaar en hebben een gebruikslimiet.
- Documentuploads (PDF, DOCX, TXT of JSON) verschijnen als signaal in de privé-admin-inbox.
- Organisatiebeheerders kunnen een upload eenmalig met Sol omzetten naar een controleerbaar formulierconcept. De app controleert letterlijke bronfragmenten, labels en opties en activeert pas na menselijke vergelijking.
- Het medewerkersdashboard toont per cliënt of alle actieve dagelijkse formulieren vandaag zijn opgeslagen.
- Fictieve cliënten kunnen worden gearchiveerd; demo-inhoud kan gecontroleerd worden gereset.

## Basisformulier dagelijkse zorg

Het organisatieformulier wordt als context aan de AI meegegeven, niet als vaste vragenlijst. De demo dekt waar relevant: welzijn, slaap, ADL, eten/drinken, toiletgang, mobiliteit, medicatie-afwijkingen, gedrag, communicatie, activiteiten, cliëntperspectief, begeleiding en effect, zorgdoelen, risico/incident/verzet, overdracht, tijdregistratie en menselijke bevestiging. De AI retourneert alle werkelijk noodzakelijke ontbrekende informatie tegelijk.

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

Voor de huidige architectuur heeft de webservice geen apart Railway Volume nodig: uploads, importconcepten, formulieren en rapportages worden versleuteld in PostgreSQL opgeslagen; tijdelijke PDF/DOCX-extractie gebeurt in geheugen. Gebruik bij grote productievolumes liever een Railway Storage Bucket/S3-opslag voor originele bestanden dan het databaseschema met grote blobs te laten meegroeien.

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

## AI-evaluaties

De fictieve evaluatieset controleert normale diensten, dunne observaties, begeleiding, medicatie, valincidenten, Wzd-verzet, acute benauwdheid en informatie van horen-zeggen. Met een tijdelijke `OPENAI_API_KEY` in de omgeving:

```bash
python scripts/eval_ai.py
```

De uitvoer bevat per scenario model, route, latency, tokens, risico, vragen, formulierselectie en pass/fail. Wijzig model, prompt of formulierrouting alleen als deze set en de gewone tests blijven slagen.

## Voor echte cliëntgegevens

Dit is een technische MVP, geen kant-en-klare wettelijke certificering. Voor productie zijn minimaal nodig: DPIA, verwerkingsregister, grondslag/doelbinding, verwerkersovereenkomsten en beoordeling doorgifte, passende OpenAI data controls/contractkeuze, NEN 7510/7512/7513-inrichting, formele autorisatiematrix, back-up/herstel, incidentrespons, bewaartermijnen/vernietiging, rechtenafhandeling, pentest, protocollen per aanbieder en inhoudelijke validatie door FG/privacyjurist, kwaliteitsfunctionaris, Wzd-functionaris en zorgprofessionals.

De wettelijke kennis in `app/legal_policy.py` is versiebeheerbare beslisondersteuning. Laat wijzigingen altijd juridisch en professioneel goedkeuren; laat het model nooit live wetten interpreteren of zelfstandig beslissen.
