# The machine's time zone, with a fallback.
#
# ⚠️ WHY THIS EXISTS. `time.timeZone` is `null or string`, and null is the
# default: a machine is allowed not to have one. Nine modules passed it
# straight into a container's `TZ` or a Grafana dashboard's location, where
# the option is typed `string`, so every one of them failed to evaluate on a
# machine that had not set a zone. The library's own base module says the
# zone is a personal value belonging in the consumer's flake, which makes
# "not set yet" an ordinary state rather than a mistake.
#
# UTC is what a container does with no TZ anyway, so the fallback changes
# nothing for a machine that has a zone and makes one work that does not.
config: if config.time.timeZone == null then "UTC" else config.time.timeZone
