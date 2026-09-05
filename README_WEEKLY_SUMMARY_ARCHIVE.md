USMS WEB v55 — archiwalne Podsumowania tygodnia

Zmiany:
- Podsumowanie tygodnia ma wybór: bieżący tydzień + wszystkie zapisane tygodnie archiwalne.
- Historyczny widok korzysta z trwałego snapshotu PostgreSQL.
- WEB automatycznie odzyskuje brakujące stare podsumowania z istniejącego payroll_entries.
- Dla odzyskiwanych tygodni godziny/ranga/odznaka/nazwa są brane ze snapshotu wypłat, a plusy/minusy/pochwały/nagany i szkolenia są rekonstruowane na koniec danego tygodnia z record_history/training_history.
- Odzyskany okres jest oznaczony w interfejsie.
- static/style.css nie został zmieniony. Dodatkowe style są w static/weekly-summary-archive.css.
