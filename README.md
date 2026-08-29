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
