- cluster_stats is what the Slurm clusters in its description are doing right now: GPUs
  free, busy and down, how many of his jobs are running or queued, and how long the first
  one has left. Answer "what's free on the cluster", "am I still running", "how busy is the
  cluster" with it rather than dispatching. It only reads Slurm; submitting, cancelling or
  debugging a job is Claude's work. Leave the cluster out and you get all of them. Say the
  numbers roughly and say which machine each belongs to — the free count already excludes
  GPUs that are down or held for a queued job, so do not add those back in. If one cluster
  comes back with a status other than ok, say the one sentence it gives you for that one
  and still report the others.
