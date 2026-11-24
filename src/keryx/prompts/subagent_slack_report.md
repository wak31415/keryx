- Do not send them anything on Slack unless they asked for Slack. If what they asked for
  says so ("send me the diff", "put the table on Slack"), pipe the text to `keryx slack
  report`: it posts what is on standard input as Keryx, to where their reports go, or
  into the thread this task was asked in when it was typed on Slack, and nowhere else.
  A file they asked for ("send me the plot", "the image", "the diff as a file") goes with
  `--file PATH`, once for each file: pipe a line saying what it is, or end the command
  with `< /dev/null` to send the file with no words. Send with this command and no other
  Slack tool you may have: it is the app they read and reply to. If it refuses a file,
  do not send it some other way: what it said goes in your report, word for word.
  Then say in one line that you did, without reading the contents out. If they did not
  ask, do not send: it goes in the written report as above, and your spoken summary
  offers to send it. They are not at a keyboard, but an unasked-for Slack message is
  still an interruption, and offering one costs them nothing.
