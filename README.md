# USMS WEB v39 — tygodnie niedziela–sobota + bonus z Discorda

Zmiany:
- Aktualny tydzień wypłat liczony jest od niedzieli do soboty.
- Strona odczytuje mnożnik x1/x2 z tej samej tabeli PostgreSQL co bot.
- Panel wypłat pokazuje stan bonusu, ale sterowanie odbywa się komendami Discord `/bonus` i `/stopbonus`.
- Archiwum wypłat pozostaje podzielone na klikalne tygodnie.

Wdrożenie: zastąp pliki w repozytorium/usłudze `usms-web-panel`. Nie zmieniaj sekretów ani DATABASE_URL.
