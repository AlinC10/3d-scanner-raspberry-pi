# Plan: Rampa de viteza + oprire lenta (`end_delay`)

> Status: IMPLEMENTAT (netestat pe hardware). Vezi `PRE_TEST_CHECKLIST.md` pentru pasii de dinainte de testare.
> Inlocuieste versiunea veche a acestui fisier (doar profil `sqrt`, fara viteza finala).

## 1. Obiectiv

1. Acceleratia si decelerarea sa fie o **rampa reala**: viteza creste/scade liniar in timp.
2. Miscarea sa se termine **foarte lent** (`end_delay`, pasi de 5-30 ms), astfel incat turntable-ul sa nu mai aiba energie cinetica
   la sfarsit, nici cu obiecte foarte grele (fara pasi/grade in plus).
3. `end_delay` este optional: **implicit oprit pe Z**, **implicit pornit pe turntable** (cu interval limitat).

## 2. Cum functioneaza (explicatie)

### Rampa de viteza vs rampa de intarziere
- Firmware-ul genereaza pasi, iar controlul este pe **intarzierea dintre pasi** (`delay`, in µs). Viteza = `1 / delay`.
- Varianta actuala (`rotateRamp`) interpoleaza liniar **intarzierea**. Viteza rezultata NU este o rampa, ci o hiperbola:
  scade/creste foarte repede la un capat si aproape deloc la celalalt. Acceleratia reala creste cu v^3.
- Varianta cu `sqrt` din planul vechi nu este "alta curba ciudata": este exact **rampa liniara in viteza** (acceleratie constanta).
  `sqrt` apare doar pentru ca bucla numara *pasi* (pozitie), nu timp: la acceleratie constanta `v^2` este liniar in numarul de pasi.

### Profilul unei miscari (cu `end_delay`)
```
viteza
  v_cruise ──────────┐───────────┐
        /            │   cruise  │ \
       /  accel      │           │  \  decel
 v_start             │           │   \
                                       v_end   <- foarte mica, ultimii pasi sunt lenti
 ────────────────────────────────────────────► pasi
```
- accel: de la `v_start = 1/start_delay` la `v_cruise = 1/delay`.
- decel: de la `v_cruise` la `v_end = 1/end_delay` (nu mai la `v_start`).
- Fara `end_delay` (Z implicit): `v_end = v_start`, ca acum, dar cu forma de rampa liniara in viteza.

### De ce rampa de viteza este necesara impreuna cu `end_delay`
Cu intervalul larg de decelerare (ex. 1 ms -> 15 ms) interpolarea liniara a intarzierii ar frana cu ~29.700 pasi/s^2 chiar la
inceputul decelerarii (aproape o oprire brusca), pentru ca acceleratia creste cu v^3. Cu rampa liniara in viteza aceeasi miscare
(640 de pasi, 1000 -> 67 pasi/s) frineaza cu ~780 pasi/s^2 (~88 grade/s^2), constant.

### Parametrii
| Parametru | Rol | Observatii |
|---|---|---|
| `delay` | viteza de croaziera (s intre pasi) | exista |
| `start_delay` | viteza de plecare a rampei de accelerare | exista (implicit `delay*3`) |
| `ramp_percent` / `decel_percent` | cat din pasi sunt in accel / decel | exista, neschimbat |
| **`end_delay`** | **intarzierea la finalul decelerarii** | **nou**, optional |

Lungimea decelerarii ramane `decel_percent` din pasi (sau `ramp_percent` daca `decel_percent` lipseste). `end_delay` schimba doar
unde ajunge viteza la finalul acelei decelerari.

## 3. Comportament si valori (decizii de confirmat)

### Constante (`hardware/motor.py`)
```python
END_DELAY_MIN = 0.005              # 5 ms  -> 200 microstep/s
END_DELAY_MAX = 0.030              # 30 ms -> 33 microstep/s
TURNTABLE_END_DELAY_DEFAULT = 0.015  # 15 ms -> 67 microstep/s
```

### Semnificatie `end_delay`
| Nivel | Implicit | Valoare `None` | Valori acceptate |
|---|---|---|---|
| `Motor.rotate/rotate_angle/rotate_distance` | `None` | oprit (decel pana la `start_delay`) | clamp in `[MIN, MAX]` |
| `Scanner.move_z_up/down*` | `None` | oprit | idem |
| `Scanner.scan(turntable_end_delay=...)` | `TURNTABLE_END_DELAY_DEFAULT` | oprit | idem |
| `Scanner.scan(z_end_delay=...)` | `None` | oprit | idem |
| Schema `TurntableRotateRequest.end_delay` | `0.015` | `null` = oprit | `ge=0.005, le=0.030` (altfel 422) |
| Schema `ZMoveRequest.end_delay` | `None` | oprit | `ge=0.005, le=0.030` (altfel 422) |

### Clamp in Python (apararea a doua, pentru apelanti interni)
```python
def _resolve_end_delay(end_delay, delay):
    if end_delay is None:
        return None
    clamped = min(max(float(end_delay), END_DELAY_MIN), END_DELAY_MAX)
    if clamped != end_delay:
        log.warning("end_delay %.4f clamped to %.4f", end_delay, clamped)
    return max(clamped, delay)   # niciodata mai rapid decat croaziera
```
- Mai mic ca minimul -> minimul. Mai mare ca maximul -> maximul. `None` -> neschimbat.
- `acceleration=False` (sau `decel` = 0 pasi): `end_delay` este ignorat si se logheaza un warning (nu exista rampa de decelerare).

## 4. Modificari

### 4.1 Firmware: `hardware/arduino_control/lib/Motor/Motor.cpp` + `Motor.h`
- Semnatura: `rotateRamp(cw, steps, targetDelayUs, startDelayUs, accelSteps, decelSteps, endDelayUs = 0)`.
  `endDelayUs == 0` -> `endDelayUs = startDelayUs` (compatibil). `endDelayUs < targetDelayUs` -> `targetDelayUs`.
- Profil (float, `sqrtf`):
```cpp
const float vS = 1e6f / startDelayUs, vT = 1e6f / targetDelayUs, vE = 1e6f / endDelayUs;
// accel (i = 0..accelSteps-1)
float v = sqrtf(vS*vS + (vT*vT - vS*vS) * (float)(i + 1) / (float)accelSteps);
// decel (i = 0..decelSteps-1), ultimul pas ajunge la vE
float v = sqrtf(vT*vT - (vT*vT - vE*vE) * (float)(i + 1) / (float)decelSteps);
uint32_t delayUs = (uint32_t)(1e6f / v);
```
- **Programare pe timp absolut** (corecteaza problema din planul vechi: calculul in bucla adauga 100-250 µs la fiecare perioada):
  `nextStepAt += delayUs`, asteptare pana `(int32_t)(micros() - nextStepAt) >= 0` (sigur la overflow `micros()`).
- **Verificarea `0xFF` se face in bucla de asteptare**, nu o data pe pas, deci latenta de oprire de urgenta ramane < 1 ms chiar
  cu pasi de 30 ms (altfel ar fi pana la 30 ms).
- Pulsul STEP ramane 50 µs (cerinta TB6600). `rotate()` (viteza constanta) ramane neschimbat.

### 4.2 Firmware: `CommandHandler.cpp`
- In ambele ramuri (`'1'` + `'6'` si `'a'/'A'`) citeste un al 8-lea argument optional `end_delay`
  (`strtoul` pe argument lipsa returneaza 0 -> inseamna "fara end_delay"). Il paseaza la `rotateRamp`.
- Comanda: `16 <id> <steps> <dir> <target_us> <start_us> <accel> <decel> [end_us]`.

### 4.3 `hardware/motor.py`
- `rotate`, `rotate_angle`, `rotate_distance`: parametru `end_delay: Optional[float] = None`, trecut mai departe.
- In `rotate()`: `end_us` din `_resolve_end_delay(...)`, trimis ca al 8-lea argument **doar daca nu este `None`** (altfel comanda
  ramane cea cu 7 argumente).
- **Timeout**: calculul actual (medie aritmetica a intarzierilor) subestimeaza timpul cand `end_delay` este mare. Pentru fiecare
  faza cu acceleratie constanta, timpul este `t = 2 * n / (v_a + v_b)`. Se inlocuieste in `expected_timeout` (pastrand `*1.5 + 3 s`).
- Actualizeaza docstring-urile (parametrii `acceleration`, `start_delay`, `ramp_percent` nu sunt "legacy ignored").

### 4.4 `hardware/scanner.py`
- `move_z_up`, `move_z_down`: `end_delay: Optional[float] = None`, trecut la `rotate`.
- `_capture_turntable_slice`: parametru `turntable_end_delay`, trecut la `turntable_motor.rotate`.
- `scan`: `turntable_end_delay: Optional[float] = TURNTABLE_END_DELAY_DEFAULT`, `z_end_delay: Optional[float] = None`.

### 4.5 `schemas/scanner.py`
- `TurntableRotateRequest.end_delay: float | None = 0.015` cu `Field(ge=END_DELAY_MIN, le=END_DELAY_MAX)`.
- `ZMoveRequest.end_delay: float | None = None` cu acelasi interval.
- Verifica inainte de implementare ca importul constantelor din `hardware.motor` in schema nu are efecte (conexiune Arduino la import);
  daca are, constantele merg intr-un modul mic fara dependente (ex. `hardware/motor_limits.py`).

### 4.6 `router/test.py` si `router/scanner.py`
- `test_turntable_rotate` si `test_z_move`: `end_delay=req.end_delay`.
- `_orchestrate_pipeline`: `turntable_end_delay=req.mechanical.turntable.end_delay`, `z_end_delay=req.mechanical.z_axis.end_delay`.

### 4.7 Documentatie
- `hardware/docs/MOTOR.md` (rampa, `end_delay`, protocol, intervalul), `hardware/docs/SCANNER.md` (`scan`, noii parametri),
  `router/docs/TEST_API.md` (campul `end_delay` la `/test/turntable/rotate` si `/test/motor/z-move`),
  `hardware/arduino_control/lib/README` (comanda `ACCEL_ROTATE` cu 8 argumente).

## 5. Ordine de marime (turntable, 1/16, 0.1125 grade/pas)

| Miscare | delay | decel | `end_delay` | acceleratie la franare | durata decel |
|---|---|---|---|---|---|
| 360 grade (3200 pasi) | 1 ms | 20% = 640 pasi | 15 ms | ~780 pasi/s^2 (~88 grade/s^2) | ~1.2 s |
| 18 grade (160 pasi) | 1 ms | 20% = 32 pasi | 15 ms | ~15.600 pasi/s^2 (~1750 grade/s^2) | ~0.1 s (**brusc**) |
| 18 grade (160 pasi) | 2 ms | 50% = 80 pasi | 15 ms | ~1.530 pasi/s^2 (~173 grade/s^2) | ~0.28 s |

Concluzie pentru scanarea cu pasi de 18 grade: pentru obiecte grele, rampa este limitata de distanta (32 de pasi), deci
`delay` mai mare si `decel_percent` mai mare (ex. 0.002 / 0.5) conteaza la fel de mult ca `end_delay`. Valorile se ajusteaza
la fata locului; miscarea de 18 grade ar dura ~0.5 s in loc de ~0.2 s.

## 6. Verificare

1. **Compilare firmware:** `pio run` in `hardware/arduino_control`.
2. **Unit tests Python (bridge mockat):** comanda trimisa are 7 argumente fara `end_delay` si 8 cu `end_delay`; clamp la min/max;
   `end_delay` ignorat cand `acceleration=False`; timeout calculat corect pentru un `end_delay` mare.
3. **Perioada pulsurilor:** masoara perioada pe pinul STEP (analizator logic) sau logheaza `micros()` pe pas intr-o rulare de test.
   Verifica ca rampele nu au salt la trecerea in croaziera si ca ultimul pas este ~`end_delay`.
4. **Fizic:** 360 grade cu obiectul cel mai greu, 3 repetari, cu `end_delay` 5/15/30 ms, plus `end_delay=null` pentru comparatie.
   Marcaj pe platou; asteptat: eroare finala in limita unui pas. Verifica si abortul (`emergency_stop`) in mijlocul rampei lente.

## 7. Puncte de confirmat inainte de implementare
1. Interval `[5 ms, 30 ms]` si implicit `15 ms` pentru turntable.
2. `end_delay: null` in schema = oprit (si pe turntable).
3. `acceleration=False` ignora `end_delay` (alternativ: il refuzam cu eroare).
4. Forma de rampa de viteza se aplica si la Z (firmware comun), doar `end_delay` este oprit implicit pe Z.
5. Lasat pentru mai tarziu: rampa definita in acceleratie (°/s^2) in loc de procente si profil triunghiular pentru miscari scurte.

## 8. Ce s-a pastrat si ce s-a schimbat fata de planul vechi
- Pastrat: formula `v = sqrt(v0^2 + (v1^2 - v0^2) * s / n)` pentru accelerare si deceleratie.
- Schimbat: decelerarea tinteste `end_delay`, nu `start_delay`; programare pe timp absolut in locul calculului in bucla.
- Eliminat: afirmatia ca overhead-ul de calcul este "absorbit" (era fals la `delay = 1 ms`, valoarea implicita din `scan()`).
