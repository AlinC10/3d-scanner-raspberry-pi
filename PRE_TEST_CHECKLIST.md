# Checklist inainte de testarea motoarelor (rampa de viteza + `end_delay`)

> Contine doar pasii **fizici** care au ramas (planuri: `plans/implemented/constant_accel_plan.md`, `plans/implemented/MOTOR_FIXES_PLAN.md`). Lista scurta a ce mai ramane in tot proiectul,
> cu valorile de reglat dupa teste, este in `plans/SCAN_CRITICAL_PATH.md` (sectiunile 2.2 si 3: M1-M9).
> **Motoarele si endstop-urile sunt netestate pe hardware**; doua scanari complete au rulat deja pe rig (20 de pasi de 18° pe inel, Z a urcat intre inele).
> Verificat deja fara hardware: 546 de teste Python (hardware mockat), testul firmware pe host (593 de verificari), build-ul pentru Arduino Uno; si ca OpenAPI arata `end_delay` implicit `0.015`
> pe `POST /test/turntable/rotate` si `null` pe `POST /test/motor/z-move`.

## 1. Inainte de prima miscare

### 1.1 Teste si firmware
```bash
python -m unittest discover -s tests -t . -p "test_*.py"     # toate testele proiectului
bash hardware/arduino_control/test/host/run.sh                # testul firmware-ului pe host: "0 failed"
cd hardware/arduino_control && pio run -t checkprogsize       # firmware-ul compileaza
```
Nu flasha daca unul dintre ele esueaza.

### 1.2 Flasheaza firmware-ul pe Arduino (obligatoriu)

> **RE-FLASH OBLIGATORIU.** Firmware-ul se schimba in `Motor.cpp` si `CommandHandler.cpp`: pinul EN trebuie sa fie HIGH (driver oprit) imediat dupa `CONFIG`, `DISABLE` / `0xFF` scriu mereu HIGH,
> iar la boot se trimite bannerul `READY` in loc de `0`. Fara re-flash, dupa pornire motoarele raman energizate, `DISABLE`/`0xFF` la repaus nu fac nimic pana la prima miscare, un reset in mijlocul unei
> miscari nu se distinge de un raspuns `OK`, iar fara rampa noua nici `end_delay` nu are efect.

1. Opreste serverul FastAPI (portul serial trebuie sa fie liber).
2. `cd hardware/arduino_control`, apoi `pio device list` (noteaza portul, de ex. `/dev/ttyACM0`).
3. `pio run -t upload`
   > `platformio.ini` are `targets = compiledb`, deci un simplu `pio run` NU compileaza si nu urca nimic, doar genereaza `compile_commands.json`. Foloseste `-t upload`.
4. Dupa `pio run`, `compile_commands.json` poate aparea modificat in `git status`; il poti ignora sau face `git checkout hardware/arduino_control/compile_commands.json`.

### 1.3 Nivelul EN la boot (obligatoriu, dupa re-flash)
Imediat dupa pornirea serverului (inainte de orice comanda de miscare, inainte de `enable`/`prepare`):

- [ ] Roteste cu mana platoul turntable-ului: trebuie sa se invarta **liber**, fara cuplu de mentinere.
- [ ] Incearca sa misti cu mana si axa Z (surubul / sania, cu atentie, sarcina camerei pe ea): trebuie sa fie libera (surubul T8 poate fi greu de invartit din cauza frecarii, dar nu trebuie sa simti cuplul de mentinere al motorului).
- [ ] `curl http://localhost:8000/test/motor/status` arata ambele motoare `disabled`.

Daca oricare motor tine cuplu imediat dupa pornire, firmware-ul vechi este inca pe placa: re-flash (1.2) si repeta. Nu continua cu testele de miscare.

### 1.4 Verificari fizice inainte de prima miscare
- [ ] Platoul este gol sau are un obiect **usor** pentru primele teste.
- [ ] Ai acces rapid la oprire. Alege dupa starea scanner-ului:
  - **in timpul unei scanari** (`preparing` / `prepared` / `running`): `curl -X POST http://localhost:8000/scanner/cancel`. `POST /test/cleanup` raspunde `409` in aceste stari si NU opreste nimic.
  - **in repaus** (`idle`, `completed`, `cancelled`, `error`, de ex. in timpul unui `/test/turntable/rotate` sau `/test/motor/z-move`): `curl -X POST http://localhost:8000/test/cleanup` (taie cuplul).
  - oricand: scoate alimentarea de 12 V.
- [ ] Surubul Z nu este la capat de cursa (`curl http://localhost:8000/test/endstops`: ambele `false` cand nu sunt apasate, deja confirmat).
- [ ] DIP-urile TB6600 ale turntable-ului sunt pe `1/16` si curentul pe valoarea dorita (vezi 3 de ce conteaza).

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
| 9 | Abort in scanare | porneste o scanare scurta (`POST /scanner/prepare`, apoi `/scanner/start` cu obiect usor) si apeleaza `POST /scanner/cancel` (NU `/test/cleanup`, care raspunde `409`) | Miscarea se opreste, starea ajunge `cancelled`, niciun motor nu ramane energizat |

(Scanarea scurta in sine a rulat deja pe rig; driftul de unghi intre nivele ramane de verificat: M9 in `plans/SCAN_CRITICAL_PATH.md`.)

La fiecare test noteaza: unghiul real fata de marcaj, zgomot anormal, caldura motor/driver, si daca Arduino-ul s-a resetat (`OSError: [Errno 5]` in log).

## 2b. Verificari de banc (pe hardware, cu platoul gol si mana pe alimentarea de 12 V)

Se fac dupa re-flash si dupa 1.3. Toate cu viteza mica (de ex. `delay` 0.002-0.003) pe Z, pentru a avea timp sa reactionezi.

### Endstop-uri
- [ ] Endstop jos: porneste o miscare Z lenta in jos (`/test/motor/z-move`, `direction: "down"`) si apasa cu mana endstop-ul jos. **Trebuie:** motorul se opreste imediat si miscarea raporteaza eroare (nu `success`).
- [ ] Endstop sus: la fel, miscare lenta in sus si apasi endstop-ul sus. **Trebuie:** oprire + eroare raportata.
- [ ] Un endstop apasat cat Z sta (nu se misca) nu opreste turntable-ul (in timpul unui `/test/turntable/rotate`): apasa-l scurt, rotatia continua.
- [ ] `GET /test/endstops` apasand pe rand fiecare switch: doar cel apasat este `true`.

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

### Limite de unghi, viteza si cursa
- [ ] `POST /scanner/start`, `mechanical.turntable.angle`: accepta `angle <= 180` care imparte rotatia completa (de ex. 18, 22.5) si respinge cu `422`: `360`, `33.75` (nu imparte 360 in pasi intregi), `10` (nu e multiplu de pas).
- [ ] `POST /test/turntable/rotate` accepta `angle` pana la `360` si respinge `> 360` si unghiuri care nu sunt multiplu de pas.
- [ ] `delay` in afara `[0.0001, 0.05]` (de ex. `0`, `1`) -> `422`; `distance_mm > 475` pe `/test/motor/z-move` -> `422`.
- [ ] Pentru scanare foloseste `delay >= 0.0005`; valori mai mici doar pentru teste manuale.

### Z dezactivat cand sta
- [ ] Z este energizat numai cat se misca. Cu camerele montate si Z dezactivat intre inele, sania **nu trebuie sa coboare** singura: lasa-o 1-2 minute dupa o miscare si verifica. Daca coboara, spune-mi: nu exista (inca) un steag de mentinere a cuplului.

## 3. Ce sa urmaresti si ce sa faci daca apare ceva

| Simptom | Cauza probabila | Ce incerci |
|---|---|---|
| Tot 10–20° in plus, cu sau fara `end_delay` | Nu e inertie, ci calibrare (DIP TB6600, pasi/tur) | Masoara 90°/180°/360°; daca excesul e proportional, verifica `1/16` pe DIP si raportul real al transmisiei |
| Excesul dispare cu `end_delay` | Era inertia la oprire (ipoteza initiala) | Ramai la `0.015`; pentru obiecte foarte grele creste la `0.020–0.030` |
| Zgomot / vibratie la finalul miscarii | Rezonanta de mijloc la pasi lenti | Schimba `end_delay` (de ex. `0.008` sau `0.012`), nu il pune la limita `0.030` |
| Pierdere de pasi in timpul rampei (la 18°) | Decelerarea e prea abrupta pe 32 de pasi | `delay` mai mare (`0.002`) si `decel_percent` mai mare (`0.5`) |
| Miscarea se opreste cu eroare / timeout | Timeout depasit | Verifica in log `rotation failed with error status ''`; creste `delay` sau spune-mi ce valori foloseai |
| Z-axis se comporta diferit fata de inainte | Forma rampei s-a schimbat si pe Z (firmware comun) | `acceleration=false` pe Z revine la viteza constanta, sau spune-mi si il tratam separat |

## 4. Ce poate surprinde
- **Forma rampei este aceeasi pentru ambele motoare** (acceleratie constanta in loc de interpolare liniara a intarzierii), nu doar pentru turntable. La `delay = 1 ms` timpul unei miscari de 18° ramane aproape acelasi (~0.22 s).
- **`end_delay` este activ implicit pe turntable** (`0.015`): in `scan()`, in `POST /scanner/start` (campul lipseste din request) si in `/test/turntable/rotate`. Pe Z este oprit implicit. `end_delay: null` il dezactiveaza; `acceleration=false` il ignora (cu warning).
- **Raspunsul `0` de la Arduino vine dupa ultima perioada de pas** (pana la 15–30 ms in plus per miscare, neglijabil).
- **Valori in afara `[0.005, 0.030]` la `end_delay`:** API-ul raspunde `422`; apelurile interne din Python sunt "clamp"-uite cu un warning in log. `delay` / `start_delay` in afara `[0.0001, 0.05]` s: `ValueError` / `422`.
- **Pauza de 0.5 s la `disable()`** ramane; se completeaza cu oprirea lenta (mentinere mecanica; efectul electric flyback / back-EMF este o ipoteza neverificata).
- **Cursa Z maxima** `475 mm` (`Z_TRAVEL_MAX_MM`; o miscare ceruta prin API poate fi cel mult atat; homing / to-top pana la 490 mm).

## 5. Decizii luate peste plan (spune-mi daca nu esti de acord)
1. In firmware, perioada minima de pas este `60 µs` (inainte `delay < 50 µs` forta `50 µs` de pauza); nu afecteaza valorile folosite acum (`>= 1 ms`).
2. `end_delay` mai mic decat `delay` (cruise) este ridicat la `delay`, ca sa nu existe "accelerare" la final.
3. Abortul cu `0xFF` este verificat si in bucla de asteptare dintre pasi (nu doar o data pe pas), ca sa ramana < 1 ms si la pasi de 30 ms.

## 6. Cum revii daca ceva nu merge
- Software: trimite `end_delay: null` in request (nu mai exista slow stop; forma rampei ramane cea noua).
- Complet: `git checkout -- hardware router schemas main_prod.py tests` aduce inapoi starea comisa (pierde modificarile necomise din acele directoare; comite-le intai); re-flasheaza apoi vechiul firmware (`pio run -t upload`).

## 7. Dupa testare
Spune-mi rezultatele (unghiul real pentru 360° / 18°, valorile `delay`/`decel_percent`/`end_delay` care au mers) ca sa fixam valorile implicite si sa decidem daca mai e nevoie de rampa definita in
acceleratie (°/s²) si profil triunghiular pentru miscari scurte.
