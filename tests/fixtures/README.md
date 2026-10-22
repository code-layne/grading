# Test fixtures

Every student here is invented. None of these files holds a real name or
real work.

## real/

`grading_scan.pdf` is a real scan. math-shared's `examples/exit_ticket` was
built for its three invented students (`examples/roster-map.csv`: K7QM2P,
T4WX9C, H3NR8V), then printed and scanned on a Brother inkjet. It holds three
pages, each the students' page 2, in reverse roster order. They were scanned
at 300 dpi as color JPEG, 2528 px wide; the scanner trims about 0.03 in from
each side.

- **Show-through:** page 1 shows through faintly, mirrored left to right: a
  ghost QR code in the top-left corner, plus ghost text and answer boxes.
  Measured on the scan's own pixels, it is darkest at 203 (isolated JPEG
  pixels; under 0.03% of the ghost-QR area is below 215). Printed ink reaches
  about 30. The ingest threshold is 160.
- **Uneven paper feed:** on page 1 the rubric box sits 7 px (1.7 pt) lower
  than the four registration marks predict. The per-box border search in
  `scan.find_border` corrects for it.
- **Rubric boxes:** all three are empty. Page 2's has one 4-pixel speck at
  gray 148.

`curriculum/target/grading/exit_ticket/manifest.json` is that component's
manifest, from `make -f grading.mk grading-manifest
COMPONENT=examples/exit_ticket` in math-shared 0ecbbee.

## synthetic/

The stand-in eval set from code-layne/algebra_2,
`unit03/lesson01/graded_sample/eval/` (12 invented students; see its
README). This copy comes from the `grading-submodule-at-root` branch, the one
with the corrected item 7 labels. It has:

- `class-set-scan.pdf`
- `scan-pages.csv`, the ground truth for which student is on each scan page
- `expected-scores.csv`, whose one `blank` row is the only box ingest should
  mark blank
- `fake-roster.csv`, the eval set's invented roster (algebra_2 main), in
  roster-map.csv's format: the report tests' roster
- `curriculum/.../manifest.json`, from `make -C unit03/lesson01 graded_sample`
- `curriculum/unit03/lesson01/rubrics/alg2-u3-l1-gs-gs8.yaml`, the gs8
  rubric the manifest names, from algebra_2 5a04b10

To refresh after the eval set is regenerated:

```sh
E=~/Mathematics/algebra_2/unit03/lesson01/graded_sample/eval
cp $E/class-set-scan.pdf $E/scan-pages.csv $E/expected-scores.csv $E/fake-roster.csv tests/fixtures/synthetic/
cp ~/Mathematics/algebra_2/target/unit03/lesson01/grading/graded_sample/manifest.json \
   tests/fixtures/synthetic/curriculum/target/unit03/lesson01/grading/graded_sample/
cp ~/Mathematics/algebra_2/unit03/lesson01/rubrics/alg2-u3-l1-gs-gs8.yaml \
   tests/fixtures/synthetic/curriculum/unit03/lesson01/rubrics/
```

## recordings/synthetic/

Not here yet. `grading eval --record tests/fixtures/recordings/synthetic`
on the Mac writes one live run over the synthetic set: one JSON file per API
call, with each crop replaced by its SHA-256. Once it is committed,
`test_recorded_live_run.py` replays it on every test run, with no network.
After a prompt changes, the replay reports which requests no longer match the
recording; record again to refresh it.

`.gitignore` ignores every PDF and CSV except the ones under
`tests/fixtures/`. It always ignores `roster-map.csv`, here too; the report
tests copy `synthetic/fake-roster.csv` to a roster-map.csv in a temporary
folder.
