# USMS Web Panel v1

Pierwsza działająca wersja panelu internetowego dla USMS.

## Co już działa

- ciemny dashboard w stylu USMS,
- logowanie przez Discord OAuth2,
- blokada dostępu dla osób spoza wskazanego serwera Discord,
- baza funkcjonariuszy z Google Sheets (`USMS`),
- profil funkcjonariusza,
- szkolenia z karty `Szkolenia`,
- akta z karty `Akta`,
- odczyt aktywnej służby, czasu, urlopu i zawieszenia z `sluzby.db`,
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

## Wspólna baza SQLite

Jeżeli bot i strona są osobnymi Railway Services, pojedynczego Railway Volume nie da się po prostu współdzielić między niezależnymi usługami tak jak zwykłego folderu. Jeżeli strona nie widzi `/data/sluzby.db`, panel nadal uruchomi się i pokaże dane z Google Sheets, ale dane czasu służby będą puste.

W następnym etapie najlepiej przenieść dane operacyjne do wspólnej bazy PostgreSQL w Railway. Wtedy bot i strona będą korzystać dokładnie z tej samej bazy i nie będzie problemu z Volume.

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
- wykonywanie akcji na Discordzie i Google Sheets z poziomu strony.


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
