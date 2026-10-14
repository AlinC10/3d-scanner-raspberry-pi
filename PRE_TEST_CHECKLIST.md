# Checklist inainte de testare: rampa de viteza + `end_delay`

> Se refera la `plans/constant_accel_plan.md` (implementat in branch-ul `worktree-end-delay-plan`, **netestat pe hardware**).
> Ce s-a verificat deja: firmware-ul compileaza (Flash 24.6%, RAM 13.2%), 10 teste Python (bridge mockat) trec,
> schemele resping valori in afara `[0.005, 0.030]`. Ce NU s-a verificat: comportamentul fizic al motorului.

## 1. Ce trebuie sa faci tu, in ordine

### 1.1 Adu modificarile in checkout-ul tau

Branch-ul `worktree-end-delay-plan` are 3 commit-uri peste `main`:

| Commit | Continut |
|---|---|
| `751517e` | planul (`plans/constant_accel_plan.md`) |
| `82695d7` | **snapshot** al modificarilor tale necomise din `hardware/motor.py`, `hardware/scanner.py`, `router/test.py`, `schemas/scanner.py` (baza pe care am lucrat) |
| ultimul | implementarea + documentatia + testele + acest fisier |

Cea mai simpla cale, fara sa-ti atingi modificarile necomise, este sa aplici doar diferenta dintre snapshot si implementare:

```bash
git diff 82695d7 worktree-end-delay-plan -- . ':!constant_accel_plan.md' ':!PRE_TEST_CHECKLIST.md' > /tmp/end_delay.patch
git apply --check /tmp/end_delay.patch   # daca da eroare, ai modificat intre timp aceleasi fisiere: spune-mi
git apply /tmp/end_delay.patch
```

Alternativ, daca preferi sa lucrezi direct din worktree: `cd .claude/worktrees/end-delay-plan` (are deja tot).
Fisierele `plans/constant_accel_plan.md` si `PRE_TEST_CHECKLIST.md` le ai (primul) sau le copiezi manual din branch:
`git show worktree-end-delay-plan:PRE_TEST_CHECKLIST.md > PRE_TEST_CHECKLIST.md`.

### 1.2 Flasheaza firmware-ul pe Arduino

Fara asta nu exista rampa noua si nici `end_delay` (firmware-ul vechi ignora al 8-lea argument, deci nu strica nimic, dar nu vei vedea nicio schimbare).

1. Opreste serverul FastAPI (portul serial trebuie sa fie liber).
2. `cd hardware/arduino_control`
3. `pio device list` (noteaza portul, de ex. `/dev/ttyACM0`)
4. `pio run -t upload`
   > `platformio.ini` are `targets = compiledb`, deci un simplu `pio run` NU compileaza si nu urca nimic, doar genereaza `compile_commands.json`. Foloseste `-t upload`.
5. Ruleaza `git status`: dupa `pio run` `compile_commands.json` poate aparea modificat; il poti ignora sau face `git checkout hardware/arduino_control/compile_commands.json`.

### 1.3 Reporneste serverul Python

Codul nou (`motor.py`, `scanner.py`, scheme, rutere) se incarca doar la repornire. Verifica in `/docs` (OpenAPI) ca
`POST /test/turntable/rotate` are campul `end_delay` (implicit `0.015`) si `POST /test/motor/z-move` are `end_delay` (implicit `null`).

### 1.4 Verificari fizice inainte de prima miscare

- [ ] Platoul este gol sau are un obiect **usor** pentru primele teste.
- [ ] Ai acces rapid la oprire: `curl -X POST http://localhost:8000/test/cleanup` (taie cuplul), sau scoate alimentarea de 12 V.
- [ ] Surubul Z nu este la capat de cursa (endstop-urile functioneaza: `curl http://localhost:8000/test/endstops`).
- [ ] DIP-urile TB6600 ale turntable-ului sunt pe `1/16` si curentul pe valoarea dorita (vezi mai jos de ce conteaza).

## 2. Secventa de teste recomandata

Toate cu serverul pornit pe `localhost:8000`. Marcheaza platoul cu un reper (banda + un creion) inainte de fiecare test.

| # | Test | Comanda | Ce ar trebui sa vezi |
|---|---|---|---|
| 1 | Fara slow stop, referinta | `curl -X POST localhost:8000/test/turntable/rotate -H 'Content-Type: application/json' -d '{"angle":360,"end_delay":null}'` | Comportamentul de dinainte (rampa de viteza), eventual cu supra-rotatia ta |
| 2 | Cu slow stop implicit | `... -d '{"angle":360}'` | Aceeasi rotatie, oprire vizibil mai lenta (ultimele ~1 s), fara grade in plus |
| 3 | Domeniu | `... -d '{"angle":360,"end_delay":0.005}'` apoi `0.030` | `0.030` mai lent la final; ambele fara grade in plus |
| 4 | In afara domeniului | `... -d '{"angle":360,"end_delay":0.001}'` | `422` (schema), nimic nu se misca |
| 5 | Unghiuri mici | `... -d '{"angle":18}'` de 5 ori, cu si fara `end_delay` | 18° curate; nu pierzi pasi, nu se aude rezonanta la final |
| 6 | Obiect greu | repeta 2 si 5 cu obiectul cel mai greu, apoi `{"angle":18,"delay":0.002,"decel_percent":0.5}` | Oprire fara alunecare; compara cu `end_delay: null` |
| 7 | Z-axis (regresie) | `curl -X POST localhost:8000/test/motor/z-move -H 'Content-Type: application/json' -d '{"distance_mm":20,"direction":"up","acceleration":true}'` si `down` | Miscare normala (end_delay implicit oprit), se opreste la capat |
| 8 | Abort in rampa | porneste 360° si apeleaza `POST /test/cleanup` in mijlocul deceleratiei lente | Motorul se opreste imediat (< 1 s), niciun reset al Arduino-ului |
| 9 | Scanare scurta | `POST /scanner/start` cu `z_move_mm` mic sau cu obiect usor | Poze la fiecare 18°, fara drift de unghi intre nivele |

La fiecare test noteaza: unghiul real fata de marcaj, zgomot anormal, caldura motor/driver, si daca Arduino-ul s-a resetat
(`OSError: [Errno 5]` in log).

## 3. Ce sa urmaresti si ce sa faci daca apare ceva

| Simptom | Cauza probabila | Ce incerci |
|---|---|---|
| Tot 10–20° in plus, cu sau fara `end_delay` | Nu e inertie, ci calibrare (DIP TB6600, pasi/tur) | Masoara 90°/180°/360°; daca excesul e proportional, verifica `1/16` pe DIP si raportul real al transmisiei |
| Excesul dispare cu `end_delay` | Era inertia la oprire (ipoteza initiala) | Ramai la `0.015`; pentru obiecte foarte grele creste la `0.020–0.030` |
| Zgomot / vibratie la finalul miscarii | Rezonanta de mijloc la pasi lenti | Schimba `end_delay` (de ex. `0.008` sau `0.012`), nu il pune la limita `0.030` |
| Pierdere de pasi in timpul rampei (la 18°) | Decelerarea e prea abrupta pe 32 de pasi | `delay` mai mare (`0.002`) si `decel_percent` mai mare (`0.5`) |
| Miscarea se opreste cu eroare / timeout | Timeout depasit | Verifica in log `rotation failed with error status ''`; creste `delay` sau spune-mi ce valori foloseai |
| Z-axis se comporta diferit fata de inainte | Forma rampei s-a schimbat si pe Z (firmware comun) | `acceleration=false` pe Z revine la viteza constanta, sau spune-mi si il tratam separat |

## 4. Ce s-a schimbat si ce poate surprinde

- **Forma rampei s-a schimbat pentru ambele motoare** (acceleratie constanta in loc de interpolare liniara a intarzierii), nu doar pentru turntable.
  La `delay = 1 ms` timpul unei miscari de 18° ramane aproape acelasi (~0.22 s).
- **`end_delay` este activ implicit pe turntable** (`0.015`): in `scan()`, in `POST /scanner/start` (campul lipseste din request) si in `/test/turntable/rotate`.
  Pe Z este oprit implicit. `end_delay: null` il dezactiveaza.
- **Raspunsul `0` de la Arduino vine acum dupa ultima perioada de pas** (pana la 15–30 ms in plus per miscare, neglijabil).
- **Valori in afara `[0.005, 0.030]`**: API-ul raspunde `422`; apelurile interne din Python sunt "clamp"-uite si se logheaza un warning.
- **`acceleration=false` ignora `end_delay`** (nu exista rampa de decelerare), cu warning in log.
- **Pauza de 0.5 s la `disable()`** (din `Motor.disable(settle_delay=0.5)`) ramane; cele doua se completeaza (oprire lenta, apoi 0.5 s cu curent).
- Nu s-au modificat: `rotate()` cu viteza constanta in firmware, pinii, logica de `scan()` in rest, ToF, camere.

## 5. Decizii luate de mine peste plan (verifica daca esti de acord)

1. In firmware, perioada minima de pas este `60 µs` (inainte `delay < 50 µs` forta `50 µs` de pauza); nu afecteaza valorile folosite acum (`>= 1 ms`).
2. `end_delay` mai mic decat `delay` (cruise) este ridicat la `delay`, ca sa nu existe "accelerare" la final.
3. Abortul cu `0xFF` este verificat si in bucla de asteptare dintre pasi (nu doar o data pe pas) ca sa ramana < 1 ms si la pasi de 30 ms.
4. Testele sunt in `tests/test_motor_end_delay.py` (unittest, fara hardware): `python -m unittest tests.test_motor_end_delay -v`.

## 6. Cum revii daca ceva nu merge

- Software: trimite `end_delay: null` in request (nu mai exista slow stop; forma rampei ramane cea noua).
- Complet: `git checkout main -- hardware/arduino_control hardware/motor.py hardware/scanner.py schemas/scanner.py router/` si re-flasheaza vechiul firmware (`pio run -t upload`).
  Atentie: acest `checkout` pierde si modificarile tale necomise din acele fisiere; comite-le intai.

## 7. Dupa testare

Spune-mi rezultatele (unghiul real pentru 360° / 18°, valorile `delay`/`decel_percent`/`end_delay` care au mers) ca sa fixam valorile implicite
si sa decidem daca mai e nevoie de rampa definita in acceleratie (°/s²) si profil triunghiular pentru miscari scurte (lasate pentru mai tarziu in plan).
