# Hardware TODOS

## Motor

* [x] **Fix angle to step conversion**: in current version, the step can be a rounded float, which will have some issues on the precision. The fix needs to calculate the steps based on the step_type given and, if a float numbers is obtained, to convert the remaining angle into another step type to get the full angle in an integer and rotate the motor in the step_type given and in the new-found step_type that will achieve the angle
* [x] **Add the Angle to Steps conversion** in @scanner.py, start_scan()