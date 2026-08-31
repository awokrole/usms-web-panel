# USMS WEB v45

Zmiany:
- administrator może usunąć każdą sesję egzaminacyjną, także aktywną i z podejściami w toku;
- usunięcie sesji kasuje kaskadowo jej podejścia, odpowiedzi i indywidualne terminy;
- przycisk „Otwórz teraz” otwiera możliwość rozpoczęcia egzaminu na 10 minut;
- sam egzamin nadal trwa 10 minut od momentu rozpoczęcia przez daną osobę;
- brak globalnego limitu liczby osób jednocześnie piszących egzamin.

## v46
- Maksymalnie 6 dokumentów na profil funkcjonariusza.
- Dokumenty pozostają w siatce 3 kolumny, więc pozycje 4-6 trafiają do drugiego rzędu.
- Limit 6 jest egzekwowany po stronie serwera; po osiągnięciu limitu formularz dodawania jest blokowany.

## v47
- Funkcjonariusze pogrupowani według stopni w ciemnych belkach zgodnych ze stylem panelu.
- Panel Admina: nowy kafelek „Podsumowanie tygodnia”.
- Podsumowanie: godziny, norma 10h, tygodniowa ocena 0–5 gwiazdek, ostatni awans, plusy/minusy/pochwały/nagany i szkolenia wymagane dla stopnia.
- Trainee: dodatkowa data przyjęcia.
- FLETC celowo pominięte w podsumowaniu szkoleń.
- Obowiązkowe: Trainee KPP; Deputy i wyżej KPP/RO/NL I/SV; Special i wyżej dodatkowo SZPIA. Szkolenia nieobowiązkowe nie obniżają kompletności.

## v48 — Ogłoszenia i Status 9 z panelu
- Panel Admina → Ogłoszenia ma duże pole tekstowe i wybór typu: Ogłoszenie / Status 9.
- Ogłoszenia publikowane z WWW są zapisywane w PostgreSQL (`web_announcements`).
- Dashboard pokazuje najnowsze ogłoszenia pod sekcją „Ostatni podgląd funkcjonariuszy”.
- „Szybki podgląd” został zastąpiony większym, przewijanym oknem „Status 9”.
- Administrator może usuwać ręcznie opublikowane wpisy; po usunięciu najnowszego Statusu 9 dashboard pokaże poprzedni (jeśli istnieje).
- Starsza historia sesji ogłoszeń bota pozostaje dostępna niżej na ekranie Ogłoszenia.


## v49 — publikacja ogłoszeń na Discord
- Panel Admina → Ogłoszenia publikuje wpis równocześnie na stronie i przez bota na Discordzie.
- Dozwolone kanały: Ogłoszenia ogólne (1511317009466396716), Ogólne informacje (1524410439381811240), Status 9 (1541198053254627488).
- Status 9 domyślnie przełącza kanał na „Status 9”; zwykłe ogłoszenie na „Ogłoszenia ogólne”. Kanał nadal można zmienić ręcznie.
- Wiadomości dłuższe niż limit Discorda 2000 znaków są automatycznie dzielone na kolejne wiadomości.
- Jeśli Discord odrzuci publikację, wpis pozostaje zapisany na stronie, a administrator zobaczy komunikat błędu.

## v50 — Discord Markdown dla ogłoszeń
- Edytor ogłoszeń dostał toolbar: B, I, U, przekreślenie, H1/H2/H3, cytat, lista i kod.
- Podgląd na żywo pokazuje formatowanie przed publikacją.
- Zachowywane są nagłówki Discord `#`, `##`, `###`, pogrubienie, kursywa, podkreślenie, przekreślenie, listy, cytaty, kod, puste linie i emoji.
- Ogłoszenia oraz Status 9 na Dashboardzie renderują ten sam Markdown w stylu zbliżonym do Discorda.
- Do Discorda nadal wysyłany jest surowy Markdown, więc Discord renderuje go natywnie.

## v51 — wzmianki funkcjonariuszy, @USMS i linki
- W edytorze ogłoszeń wpisanie `@` pokazuje podpowiedzi aktywnych funkcjonariuszy; można szukać po numerze odznaki lub imieniu/nazwisku.
- Wybranie funkcjonariusza wstawia `@ODZNAKA`; podczas publikacji WEB zamienia ją na prawdziwe `<@DISCORD_ID>` i zezwala Discordowi na ping tej osoby.
- `@usms` jest zamieniane na ping roli `1511317008720068650`.
- Na stronie skróty wzmianek są renderowane czytelnie w stylu Discorda.
- Adresy `http://` i `https://` są klikalnymi linkami w podglądzie, na Dashboardzie i w historii ogłoszeń.
- Lista kanałów `#kanał` nie jest jeszcze dodana — będzie osobnym kolejnym etapem.


## v52 — czytelne wzmianki na stronie
- Wzmianki funkcjonariuszy wpisywane jako `@721` są na stronie renderowane jako `[721] Imię Nazwisko`.
- Zmiana obejmuje podgląd edytora, Dashboard oraz historię ogłoszeń.
- Na Discord nadal wysyłany jest prawdziwy ping użytkownika na podstawie Discord ID; mechanizm pingowania nie został zmieniony.
- `@usms` nadal renderuje się jako `@USMS` i pinguje rolę USMS na Discordzie.

## v53 — edycja i usuwanie zsynchronizowane z Discordem
- nowe ogłoszenia zapisują `discord_channel_id` oraz wszystkie `discord_message_ids`,
- przycisk **Edytuj** aktualizuje wpis na stronie i tę samą wiadomość / wiadomości na Discordzie,
- edycja ma wyłączone `allowed_mentions`, więc nie wysyła ponownego pingu,
- jeśli długość wpisu po edycji wymaga innej liczby wiadomości Discord, panel tworzy brakujące lub usuwa nadmiarowe fragmenty,
- **Usuń** usuwa wpis ze strony oraz powiązane wiadomości Discord (404 na Discordzie jest traktowane jako już usunięte),
- jednorazowa migracja podpina ostatni historyczny Status 9 do kanału `1541198053254627488` i wiadomości `1543886109308755968`, `1543886110848196628`.
