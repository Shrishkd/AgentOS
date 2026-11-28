#!/bin/sh
set -eu
umask 077
id=${2:?supply a unique backup ID}
case "$id" in .|..|*[!a-zA-Z0-9_-]*) echo 'Invalid backup ID' >&2; exit 2;; esac
case "$1" in
    backup)
        # Reserve the ID before copying. Only a verified backup gets a complete marker.
        mkdir "/backups/$id"
        pg_basebackup --checkpoint=fast --wal-method=stream --pgdata="/backups/$id/data" --username=postgres
        pg_verifybackup "/backups/$id/data"
        touch "/backups/$id/complete"
        ;;
    restore)
        point=${3:?supply a named restore point or latest}
        case "$point" in *[!a-zA-Z0-9_-]*) echo 'Invalid restore point' >&2; exit 2;; esac
        test -f "/backups/$id/complete"
        test -z "$(ls -A "$PGDATA")" || { echo 'Restore requires an empty data volume' >&2; exit 1; }
        pg_verifybackup "/backups/$id/data"
        cp -a "/backups/$id/data/." "$PGDATA/"
        chmod 0700 "$PGDATA"
        include="include = 'agentos-recovery.conf'"
        grep -Fqx "$include" "$PGDATA/postgresql.conf" || echo "$include" >> "$PGDATA/postgresql.conf"
        cat > "$PGDATA/agentos-recovery.conf" <<EOF
restore_command = 'cp /archive/%f %p'
recovery_target_action = 'promote'
EOF
        if [ "$point" != latest ]; then
            echo "recovery_target_name = '$point'" >> "$PGDATA/agentos-recovery.conf"
        fi
        touch "$PGDATA/recovery.signal"
        ;;
    *) exit 2;;
esac
