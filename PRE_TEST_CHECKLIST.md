# Checklist inainte de testare: rampa de viteza + `end_delay`

> Se refera la `constant_accel_plan.md` si la `plans/implemented/MOTOR_FIXES_PLAN.md` (runda 3). **Netestat pe hardware.**
> Ce s-a verificat deja: 185 de teste Python (hardware mockat), testul firmware pe host (593 de verificari), build-ul pentru Arduino Uno,
> plus o verificare independenta in cod. Ce NU s-a verificat: comportamentul fizic al motoarelor si al endstop-urilor.
>
> **Runda 3** (`plans/implemented/MOTOR_FIXES_PLAN.md`): firmware, `motor.py`, `scanner.py`, scheme si rutere au fost modificate din nou (vezi sectiunea 8 de mai jos
> pentru verificarile de banc noi). Deciziile care prevaleaza sunt in `plans/implemented/MOTOR_FIXES_PLAN.md` sectiunea 3.1.

## 1. Ce trebuie sa faci tu, in ordine

### 1.1 Starea codului in proiect

Fisierele modificate in rundele 1-3 sunt **deja copiate in proiect** (nu mai trebuie aplicat niciun patch si nu s-a facut niciun commit).
Pentru a vedea exact ce s-a schimbat, ruleaza `git status` in radacina proiectului. Fisierele principale:

| Zona | Fisiere |
|---|---|
| Firmware (re-flash!) | `hardware/arduino_control/lib/Motor/Motor.cpp`, `Motor.h`, `lib/CommandHandler/CommandHandler.cpp`, test pe host in `hardware/arduino_control/test/host/` |
| Python hardware | `hardware/motor.py`, `hardware/arduino_bridge.py`, `hardware/scanner.py` |
| API | `schemas/scanner.py`, `router/scanner.py`, `router/test.py`, `main_prod.py` |
| Teste | `tests/test_motor_end_delay.py`, `tests/test_scanner_motion_safety.py`, `tests/test_medium_fixes.py`, `tests/test_schema_validation.py`, `tests/test_ws_b_motor_bridge.py`, `tests/test_ws_c_schemas_router.py`, `tests/test_ws_d_scanner.py` |
| Documentatie | `hardware/docs/*.md`, `router/docs/*.md`, `plans/implemented/MOTOR_FIXES_PLAN.md`, `plans/implemented/BUGS_MOTORS_ENDSTOPS.md`, acest fisier |

Ruleaza testele din radacina proiectului:

```bash
python -m unittest tests.test_scanner_motion_safety tests.test_medium_fixes tests.test_motor_end_delay tests.test_scanner_camera_mode \
    tests.test_schema_validation tests.test_ws_b_motor_bridge tests.test_ws_c_schemas_router tests.test_ws_d_scanner
```

### 1.2 Flasheaza firmware-ul pe Arduino

> **RE-FLASH OBLIGATORIU.** Runda 3 schimba firmware-ul (`Motor.cpp`, `CommandHandler.cpp`): pinul EN trebuie sa fie HIGH (driver oprit) imediat dupa `CONFIG`,
> iar `DISABLE` / `0xFF` trebuie sa scrie mereu HIGH (fix A1). Fara re-flash, dupa pornire motoarele raman energizate si `DISABLE`/`0xFF` la repaus nu fac nimic
> pana la prima miscare. Fara rampa noua nici `end_delay` nu are efect.

**Verificare inainte de flash** (pe host, fara hardware): ruleaza testul firmware-ului compilat cu un `Arduino.h` fals si cere sa treaca:

```bash
bash hardware/arduino_control/test/host/run.sh
```

Verifica in plus ca firmware-ul compileaza: `cd hardware/arduino_control && pio run -t checkprogsize`. Nu flasha daca unul dintre ele esueaza.

1. Opreste serverul FastAPI (portul serial trebuie sa fie liber).
2. `cd hardware/arduino_control`
3. `pio device list` (noteaza portul, de ex. `/dev/ttyACM0`)
4. `pio run -t upload`
   > `platformio.ini` are `targets = compiledb`, deci un simplu `pio run` NU compileaza si nu urca nimic, doar genereaza `compile_commands.json`. Foloseste `-t upload`.
5. Ruleaza `git status`: dupa `pio run` `compile_commands.json` poate aparea modificat; il poti ignora sau face `git checkout hardware/arduino_control/compile_commands.json`.

> **Re-flash obligatoriu si dupa remedierile din `plans/implemented/BUGS_MOTORS_ENDSTOPS.md` (sectiunea "Probleme suplimentare")**: firmware-ul trimite acum bannerul `READY` la boot in loc de `0`. Cu firmware vechi totul functioneaza, dar un reset in mijlocul unei miscari nu poate fi distins de un raspuns `OK`.

### 1.3 Reporneste serverul Python

Codul nou (`motor.py`, `scanner.py`, scheme, rutere) se incarca doar la repornire. Verifica in `/docs` (OpenAPI) ca
`POST /test/turntable/rotate` are campul `end_delay` (implicit `0.015`) si `POST /test/motor/z-move` are `end_delay` (implicit `null`).

### 1.3b Nivelul EN la boot (obligatoriu, dupa re-flash)

Imediat dupa pornirea serverului (inainte de orice comanda de miscare, inainte de `enable`/`prepare`):

- [ ] Roteste cu mana platoul turntable-ului: trebuie sa se invarta **liber**, fara cuplu de mentinere.
- [ ] Incearca sa misti cu mana si axa Z (surubul / sania, cu atentie, sarcina camerei pe ea): trebuie sa fie libera (surubul T8 poate fi greu de invartit din cauza frecarii, dar nu trebuie sa simti cuplul de mentinere al motorului).
- [ ] `curl http://localhost:8000/test/motor/status` arata ambele motoare `disabled`.

Daca oricare motor tine cuplu imediat dupa pornire, firmware-ul vechi este inca pe placa: re-flash (sectiunea 1.2) si repeta. Nu continua cu testele de miscare.

### 1.4 Verificari fizice inainte de prima miscare

- [ ] Platoul este gol sau are un obiect **usor** pentru primele teste.
- [ ] Ai acces rapid la oprire. Alege dupa starea scanner-ului:
  - **in timpul unei scanari** (`preparing` / `prepared` / `running`): `curl -X POST http://localhost:8000/scanner/cancel`. `POST /test/cleanup` raspunde `409` in aceste stari si NU opreste nimic.
  - **in repaus** (`idle`, `completed`, `cancelled`, `error`, de ex. in timpul unui `/test/turntable/rotate` sau `/test/motor/z-move`): `curl -X POST http://localhost:8000/test/cleanup` (taie cuplul).
  - oricand: scoate alimentarea de 12 V.
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
| 8 | Abort in rampa | porneste 360° prin `/test/turntable/rotate` (scanner in repaus) si apeleaza `POST /test/cleanup` in mijlocul deceleratiei lente | Motorul se opreste imediat (< 1 s), niciun reset al Arduino-ului |
| 9 | Scanare scurta | `POST /scanner/start` cu `z_move_mm` mic sau cu obiect usor | Poze la fiecare 18°, fara drift de unghi intre nivele |
| 10 | Abort in scanare | in timpul testului 9 apeleaza `POST /scanner/cancel` (NU `/test/cleanup`, care raspunde `409`) | Miscarea se opreste, starea ajunge `cancelled`, niciun motor nu ramane energizat |

La fiecare test noteaza: unghiul real fata de marcaj, zgomot anormal, caldura motor/driver, si daca Arduino-ul s-a resetat
(`OSError: [Errno 5]` in log).

## 2b. Verificari de banc (runda 3, pe hardware, cu platoul gol si mana pe alimentarea de 12 V)

Se fac dupa re-flash si dupa sectiunea 1.3b. Toate cu viteza mica (de ex. `delay` 0.002-0.003) pe Z, pentru a avea timp sa reactionezi.

### Endstop-uri

- [ ] Endstop jos: porneste o miscare Z lenta in jos (`/test/motor/z-move`, `direction: "down"`) si apasa cu mana endstop-ul jos. **Trebuie:** motorul se opreste imediat si miscarea raporteaza eroare (nu `success`).
- [ ] Endstop sus: la fel, miscare lenta in sus si apasi endstop-ul sus. **Trebuie:** oprire + eroare raportata.
- [ ] Un endstop apasat cat Z sta (nu se misca) nu opreste turntable-ul (in timpul unui `/test/turntable/rotate`): apasa-l scurt, rotatia continua.

### Directia Z

- [ ] Cu o miscare Z mica (`distance_mm: 5`, `delay` 0.003, `direction: "up"`) si cu mana pe alimentarea de 12 V, confirma ca sania urca (sus = spre endstop-ul de sus). Daca merge invers, opreste imediat: directia este inversata (`clockwise` / DIR) si homing-ul ar impinge in capatul gresit.
- [ ] Repeta cu `direction: "down"` si confirma ca coboara.

### Cancel in toate fazele

Pentru fiecare faza, apeleaza `POST /scanner/cancel` si confirma ca miscarea curenta se opreste si **nicio miscare noua nu porneste** dupa cancel (motoarele raman libere, starea este `cancelled`):

- [ ] in timpul homing-ului (`/scanner/prepare`, cat Z coboara spre endstop-ul jos)
- [ ] in timpul pauzei de 0.5 s (dupa o miscare, inainte ca motorul sa fie dezactivat)
- [ ] in timpul urcarii Z intre inele
- [ ] in timpul unui inel (rotatii turntable)

### Cereri suprapuse

- [ ] Doua cereri `POST /test/motor/z-move` aproape simultan (de ex. doua `curl` in fundal, cu `distance_mm: 20`): **a doua trebuie sa raspunda `409`**, iar Z executa o singura miscare.

### Limite de unghi

- [ ] Configuratia de scanare (`POST /scanner/start`, `mechanical.turntable.angle`) accepta `angle <= 180` care imparte rotatia completa (de ex. 18, 22.5) si respinge cu `422`: `360`, `33.75` (nu imparte 360 in pasi intregi), `10` (nu e multiplu de pas).
- [ ] `POST /test/turntable/rotate` accepta `angle` pana la `360` (decizia D1) si respinge `> 360` si unghiuri care nu sunt multiplu de pas.

### Limite de viteza si cursa

- [ ] `delay` in afara `[0.0001, 0.05]` (de ex. `0`, `1`) -> `422`.
- [ ] `distance_mm > 150` pe `/test/motor/z-move` -> `422`.
- [ ] Pentru scanare foloseste `delay >= 0.0005` (decizia D4); valori mai mici doar pentru teste manuale.

### Z dezactivat cand sta

- [ ] Z este energizat numai cat se misca (decizia D2). Cu camerele montate si Z dezactivat intre inele, sania **nu trebuie sa coboare** singura: lasa-o 1-2 minute dupa o miscare si verifica. Daca coboara, spune-mi: nu exista (inca) un steag de mentinere a cuplului.

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
- **Pauza de 0.5 s la `disable()`** (din `Motor.disable(settle_delay=0.5)`) ramane; cele doua se completeaza (oprire lenta, apoi 0.5 s cu curent). Este o mentinere mecanica; efectul electric (flyback / back-EMF) este o ipoteza neverificata.
- **Limite noi (runda 3):** `delay` / `start_delay` in `[0.0001, 0.05]` s (`ValueError` / `422` in afara), recomandare de scanare `delay >= 0.0005`; cursa Z maxima `150 mm` (homing / to-top pana la 165 mm).
- Nu s-au modificat: `rotate()` cu viteza constanta in firmware, pinii, logica de `scan()` in rest, ToF, camere.

## 5. Decizii luate de mine peste plan (verifica daca esti de acord)

1. In firmware, perioada minima de pas este `60 µs` (inainte `delay < 50 µs` forta `50 µs` de pauza); nu afecteaza valorile folosite acum (`>= 1 ms`).
2. `end_delay` mai mic decat `delay` (cruise) este ridicat la `delay`, ca sa nu existe "accelerare" la final.
3. Abortul cu `0xFF` este verificat si in bucla de asteptare dintre pasi (nu doar o data pe pas) ca sa ramana < 1 ms si la pasi de 30 ms.
4. Testele sunt in `tests/test_motor_end_delay.py` (unittest, fara hardware): `python -m unittest tests.test_motor_end_delay -v`.

## 6. Cum revii daca ceva nu merge

- Software: trimite `end_delay: null` in request (nu mai exista slow stop; forma rampei ramane cea noua).
- Complet: `git checkout -- hardware router schemas main_prod.py tests` aduce inapoi starea comisa; re-flasheaza apoi vechiul firmware (`pio run -t upload`).
  Atentie: pierde modificarile necomise din acele directoare; comite-le intai daca vrei sa le pastrezi.

## 7. Dupa testare

Spune-mi rezultatele (unghiul real pentru 360° / 18°, valorile `delay`/`decel_percent`/`end_delay` care au mers) ca sa fixam valorile implicite
si sa decidem daca mai e nevoie de rampa definita in acceleratie (°/s²) si profil triunghiular pentru miscari scurte (lasate pentru mai tarziu in plan).
