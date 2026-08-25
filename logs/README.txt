Log folders
===========

  app          Application logs, one file per day. Start here.
  errors       Errors only, one JSON object per line. Machine-readable.
  runs         One file per command or window session, with its settings and result.
  install      Installer transcripts, one per run.
  crash        Unhandled crash reports.
  diagnostics  Diagnostic bundles produced by 'app.cli diagnose'.

If something goes wrong, run this and send the resulting zip:

    venv\Scripts\python.exe -m app.cli diagnose

It gathers the environment report, both store summaries, recent
logs and the installed package versions into one file.

See docs/TROUBLESHOOTING.md for what each file means.
