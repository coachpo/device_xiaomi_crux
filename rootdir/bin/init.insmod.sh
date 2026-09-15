#!/vendor/bin/sh

# init.insmod.cfg contains action|argument entries. Readiness properties are
# milestones in that file and must only be set after preceding commands succeed.
set -e

if [ "$#" -ne 1 ] || [ ! -r "$1" ]; then
    echo "Missing or unreadable init.insmod.cfg" >&2
    exit 1
fi

while IFS="|" read -r action arg || [ -n "$action" ]; do
    case "$action" in
        ""|\#*) continue ;;
        insmod) insmod "$arg" ;;
        setprop) setprop "$arg" 1 ;;
        enable) echo 1 > "$arg" ;;
        # The argument is a whitespace-separated list of module names.
        modprobe) modprobe -a -d /vendor/lib/modules $arg ;;
        *)
            echo "Unknown init.insmod.cfg action: $action" >&2
            exit 1
            ;;
    esac
done < "$1"
