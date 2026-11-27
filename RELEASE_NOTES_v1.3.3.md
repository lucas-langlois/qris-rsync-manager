# QRIS Rsync Manager v1.3.3

## Changed

- **Archive names show what is inside.** Automatic archives are now named
  after the folder and the file types they hold, for example `PILOT_txt.tar`
  or `Dive photos_jpg_json.tar`, instead of just `PILOT.tar`. An archive holds
  only the folder's files of those types (plus files that share their names),
  so the old name wrongly suggested the whole folder. Parts are named
  `Videos TS_mp4.part01.tar` and so on.
- **Old archives are still recognised.** **Synchronise…** treats an
  up-to-date archive uploaded under the old folder-only name as current, so it
  is not sent again under the new name.
