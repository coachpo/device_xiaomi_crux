#!/vendor/bin/sh

# Write one ID per cgroup operation; each thread has its own membership.
ps -AT -o TID,CMD | while read -r tid name; do
    case "$name" in
        system_server)
            echo "$tid" > /dev/cpuset/foreground/cgroup.procs
            echo "$tid" > /dev/stune/foreground/cgroup.procs
            ;;
        android.io|android.fg)
            echo "$tid" > /dev/stune/foreground/tasks
            ;;
        android.anim|android.anim.lf)
            echo "$tid" > /dev/cpuset/top-app/tasks
            ;;
        android.ui|reclaimd)
            echo "$tid" > /dev/stune/top-app/tasks
            ;;
        android.display)
            echo "$tid" > /dev/cpuset/top-app/tasks
            echo "$tid" > /dev/stune/top-app/tasks
            ;;
    esac
done
