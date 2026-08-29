# USMS Web Panel v19

Panel internetowy USMS — PostgreSQL jako źródło danych panelu.

## Co już działa

- ciemny dashboard w stylu USMS,
- logowanie przez Discord OAuth2,
- blokada dostępu dla osób spoza wskazanego serwera Discord,
- baza funkcjonariuszy z PostgreSQL,
- profil funkcjonariusza,
- szkolenia z PostgreSQL (`officer_trainings`),
- akta z PostgreSQL (`officer_records`),
- odczyt aktywnej służby, czasu, urlopu i zawieszenia z tabeli `users` PostgreSQL,
- podstawowy podział zwykły użytkownik / administrator strony.

## Ważne

Panel jest na razie **tylko do odczytu**. To celowe — najpierw uruchamiamy bezpieczne logowanie i wyświetlanie danych, a dopiero potem dodamy przyciski typu Awans / Plus / Urlop.

## Railway — szybkie uruchomienie

1. Utwórz nowy Service w tym samym Railway Project.
2. Wgraj ten projekt do GitHub albo użyj repozytorium z tymi plikami.
3. Start Command może być pusty, bo jest `Procfile`. Alternatywnie:
   `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Dodaj zmienne z `.env.example`.
5. W Railway wygeneruj domenę dla nowego serwisu.
6. Ustaw `PUBLIC_URL` dokładnie na tę domenę, bez końcowego `/`.
7. W Discord Developer Portal → Twoja aplikacja → OAuth2 → Redirects dodaj:
   `https://TWOJA-DOMENA/auth/discord/callback`
8. Wstaw Client ID do `DISCORD_CLIENT_ID`.
9. Client Secret wpisz **tylko w Railway** jako `DISCORD_CLIENT_SECRET`.
10. Wpisz ID serwera do `DISCORD_GUILD_ID`.

## Discord bot token

`DISCORD_TOKEN` jest używany przez stronę wyłącznie do sprawdzenia, czy zalogowana osoba należy do serwera i jakie ma role.

Nie wpisuj tokenu do kodu ani nie wysyłaj go nikomu.

## Wspólna baza PostgreSQL

Web i bot korzystają z tej samej usługi PostgreSQL na Railway. Panel nie potrzebuje Google Sheets do wyświetlania Funkcjonariuszy, Szkoleń ani Akt. SQLite `DB_PATH` może pozostać przy bocie jako kopia/fallback, ale produkcyjny web korzysta z `DATABASE_URL`.

## Role administracyjne

W `WEB_ADMIN_ROLE_IDS` wpisz ID ról, które mają dostać dostęp do sekcji administracyjnych, np.:

`123456789012345678,987654321098765432`

Jeśli zmienna jest pusta, nikt nie otrzyma uprawnień administratora strony.

## Następny etap

- logi komend na stronie,
- wypłaty,
- urlopy i zawieszenia,
- statusy na żywo,
- zarządzanie funkcjonariuszem z panelu,
- przełączenie pozostałych komend kadrowych bota z Google Sheets na PostgreSQL.


## PostgreSQL (v2)

Na Railway ustaw `DATABASE_URL=${{database.DATABASE_URL}}`.
Panel czyta tabelę `users` z tej samej bazy PostgreSQL co bot.
`DB_PATH` nie jest potrzebne na produkcyjnym serwisie WWW.


## v3 — czas bieżącej służby

Sekcja „Aktualnie na służbie” pokazuje teraz wyłącznie czas od ostatniego
START SŁUŻBY. Karta „Łączne godziny” nadal sumuje zapisany czas historyczny
oraz trwającą aktualnie służbę.


## v4 — godziny tygodniowe i łączne

- `ŁĄCZNE GODZINY W TYGODNIU` korzystają z `total_seconds` i resetują się po `/resetgodzin`.
- `ŁĄCZNE GODZINY` korzystają z `lifetime_seconds` i nie są resetowane.
- Trwająca służba jest doliczana na żywo do obu liczników.


## v5 — profile, zdjęcia i dokumenty

- Każdy profil funkcjonariusza pokazuje łączny czas służby (`lifetime_seconds`).
- Zdjęcia profilu i screenshoty dokumentów są przechowywane trwale w PostgreSQL.
- Dokumenty mogą oglądać zalogowani użytkownicy panelu.
- Dodawanie/usuwanie zdjęć i dokumentów jest dostępne tylko dla administratora.
- Administrator jest wykrywany po uprawnieniu Discord `Administrator`, właścicielu serwera
  albo opcjonalnie po `WEB_ADMIN_ROLE_IDS`.
- Zdjęcie profilu: do 5 MB.
- Dokument/screenshot: do 10 MB.


## v6 — drag & drop

Zdjęcie funkcjonariusza oraz screenshot dokumentu można teraz:
- przeciągnąć bezpośrednio na pole uploadu,
- albo kliknąć pole i wybrać plik normalnie.

Po upuszczeniu pliku panel pokazuje jego nazwę przed wysłaniem.


## v7 — poprawka drag & drop

Naprawiono błąd, przez który przeciągnięte zdjęcie otwierało się w nowej karcie.
Skrypt drag & drop jest teraz uruchamiany po załadowaniu strony i blokuje
domyślne zachowanie przeglądarki dla upuszczanych plików.


## v8 — wklejanie screenshotów przez Ctrl+V

W formularzu zdjęcia profilowego oraz dokumentu można teraz:
- przeciągnąć obraz,
- kliknąć i wybrać plik,
- wkleić screenshot bezpośrednio ze schowka przez `Ctrl+V`.

Po `Win + Shift + S` wystarczy kliknąć odpowiednie pole uploadu i nacisnąć `Ctrl+V`.
Panel automatycznie tworzy nazwę typu `screenshot-2026-...png`.


## v9 — upload dla wszystkich, usuwanie tylko dla administratora

- Każdy zalogowany użytkownik panelu może dodać lub zmienić zdjęcie funkcjonariusza.
- Każdy zalogowany użytkownik panelu może dodać screenshot dokumentu.
- Tylko administrator może usuwać zdjęcie lub dokument.
- Drag & drop oraz Ctrl+V nadal działają.


## v10 — dodawanie tylko na własnym profilu

- Zalogowany użytkownik może dodać lub zmienić zdjęcie tylko na profilu,
  którego `Discord ID` w rosterze jest zgodne z jego kontem Discord.
- Dokument/screenshot można dodać tylko do własnego profilu.
- Próba ręcznego wysłania pliku do cudzego profilu kończy się HTTP 403.
- Tylko administrator może usuwać zdjęcia i dokumenty.
- Drag & drop oraz Ctrl+V nadal działają.


## v11 — Kompendium
Dodano zakładkę Kompendium na bazie v10, zachowując ograniczenie dodawania zdjęć i dokumentów wyłącznie do własnego profilu.


## v14 — korekta prawa do telefonu
Usunięto z Kompendium informację o 2-minutowym prawie zatrzymanego do telefonu oraz usunięto odpowiadające temu pytanie z banku egzaminacyjnego. Zatrzymanemu nie przysługuje obecnie prawo do telefonu.


## v15 — pula 300 pytań
- Pula egzaminacyjna została rozszerzona do 300 pozycji na bazie zatwierdzonej wiedzy z Kompendium.
- Warianty pytań nie wprowadzają nowych zasad — zmieniają sposób sformułowania sprawdzanego faktu.
- Jeden egzamin nadal losuje 20 pytań.
- Backend pilnuje, aby w jednym podejściu nie pojawiły się dwa warianty tego samego faktu (ta sama odpowiedź wzorcowa).


## v16 — równoczesne egzaminy + 10 minut
- Wielu funkcjonariuszy może rozpocząć tę samą sesję egzaminacyjną równocześnie; każde podejście jest osobne i przypisane do Discord ID.
- Czas pojedynczego egzaminu: 10 minut.
- Istniejące aktywne/przyszłe sesje są automatycznie aktualizowane do 10 minut przy starcie aplikacji.


## v17 — poprawka indywidualnych terminów
- naprawiono pustą listę funkcjonariuszy w formularzu (używany jest `full_name`, a nie nieistniejące `name`),
- dodano awaryjne/uzupełniające pobieranie funkcjonariuszy z PostgreSQL `users`,
- formularz indywidualnego terminu jest responsywny i nie wychodzi poza panel.

## V18 — usuwanie starych egzaminów
- administrator może trwale usunąć zakończoną sesję egzaminacyjną,
- usunięcie sesji usuwa także jej podejścia, zapisane odpowiedzi oraz indywidualne terminy/poprawki,
- bank pytań pozostaje nienaruszony,
- backend blokuje usunięcie aktywnej sesji oraz sesji, w której ktoś nadal pisze egzamin,
- operacja jest dostępna wyłącznie dla administratora, przez POST i z walidacją CSRF.

## v19 — PostgreSQL zamiast Google Sheets

Panel nie wykonuje już żadnych odczytów z Google Sheets podczas normalnego działania. Funkcjonariusze, szkolenia i akta są odczytywane z PostgreSQL. Do paczki dołączony jest `seed_database_usms.json`, czyli jednorazowy snapshot danych z przesłanego `DATABASE USMS.xlsx` z 29.08.2026. Przy pierwszym uruchomieniu v19 dane są automatycznie importowane do tabel `officers`, `officer_trainings`, `officer_records` i `payroll_entries`, a migracja jest oznaczana w `web_migrations`, więc nie uruchamia się ponownie.

Aktualna odznaka i aktywność są dodatkowo synchronizowane z tabelą `users` używaną przez bota: jeżeli bot ma nowszy numer odznaki, panel użyje go; jeżeli istniejący użytkownik ma wyczyszczoną odznakę, nie pojawia się na aktywnej liście. Stopień jest wyliczany z obowiązujących zakresów odznak 701–799.

Zmienne `GOOGLE_TOKEN_JSON`, `SHEET_ID`, `ROSTER_SHEET_NAME`, `TRAINING_SHEET_NAME` i `AKTA_SHEET_NAME` nie są już potrzebne w usłudze WEB. Nie usuwaj ich jeszcze z usługi bota, dopóki komendy kadrowe bota nie zostaną przełączone w całości na PostgreSQL.

## v20 — wspólna baza kadrowa z botem v91
- `officers` jest wspólną listą zatrudnionych dla panelu i bota.
- `/add` w bocie v91 tworzy/reaktywuje funkcjonariusza w PostgreSQL.
- `/zwolnij` ustawia `active=false`; historia nie jest kasowana.
- szkolenia są w `officer_trainings`, a plusy/minusy/pochwały/nagany w `officer_records`.
- dodano audyt: `officer_history`, `training_history`, `record_history`.
- Google Sheets nie jest używane przez panel WWW.
