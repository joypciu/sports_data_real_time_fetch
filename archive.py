import os
import json
import duckdb
from datetime import datetime, timedelta

# Configuration
DB_PATH = "db/sports.db"
ARCHIVE_DIR = "archive"
SIZE_THRESHOLD_GB = 5  # Archive if DB exceeds 5 GB
MONTHS_TO_KEEP = 2  # Keep data newer than 2 months


def get_db_size_gb():
    """Get database size in GB."""
    size_bytes = os.path.getsize(DB_PATH)
    return size_bytes / (1024**3)


def get_cutoff_date():
    """Calculate cutoff date (today - MONTHS_TO_KEEP months)."""
    today = datetime.now()
    cutoff = today - timedelta(days=30 * MONTHS_TO_KEEP)  # Approximate
    return cutoff.strftime("%Y-%m-%d")


def ensure_archive_dir():
    """Ensure archive directory exists."""
    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)


def archive_table(
    conn, table_name, date_column=None, join_table=None, join_condition=None
):
    """
    Archive old records from a table to JSON and delete from DB.

    Args:
        conn: DuckDB connection
        table_name: Name of table to archive
        date_column: Date column to use for filtering (if table has direct date)
        join_table: Table to join with for date filtering (if no direct date)
        join_condition: Join condition (e.g., "table1.id = table2.foreign_id")
    """
    cutoff_date = get_cutoff_date()

    # Build query to select old records
    if date_column:
        # Table has direct date column
        select_query = f"""
            SELECT * FROM {table_name}
            WHERE {date_column} < ?
        """
        params = [cutoff_date]
    elif join_table and join_condition:
        # Need to join to get date
        select_query = f"""
            SELECT t1.* FROM {table_name} t1
            JOIN {join_table} t2 ON {join_condition}
            WHERE t2.game_date < ?
        """
        params = [cutoff_date]
    else:
        raise ValueError(
            "Either date_column or join_table/join_condition must be provided"
        )

    # Execute select
    result = conn.execute(select_query, params).fetchall()
    if not result:
        print(f"No old records found in {table_name}")
        return 0

    # Get column names
    columns = [desc[0] for desc in conn.description]

    # Convert to list of dicts
    records = [dict(zip(columns, row)) for row in result]

    # Write to JSON file
    archive_path = os.path.join(ARCHIVE_DIR, f"old_{table_name}.json")
    with open(archive_path, "w") as f:
        json.dump(records, f, indent=2, default=str)

    print(f"Archived {len(records)} records from {table_name} to {archive_path}")

    # Delete old records from table
    if date_column:
        delete_query = f"""
            DELETE FROM {table_name}
            WHERE {date_column} < ?
        """
    else:
        delete_query = f"""
            DELETE FROM {table_name}
            WHERE EXISTS (
                SELECT 1 FROM {join_table} t2
                WHERE {join_condition} AND t2.game_date < ?
            )
        """

    conn.execute(delete_query, params)
    conn.commit()  # Commit after each table

    return len(records)


def main():
    """Main archiving process."""
    print(f"Checking database size: {DB_PATH}")

    # Check if archiving is needed
    size_gb = get_db_size_gb()
    print(f"Current database size: {size_gb:.2f} GB")

    if size_gb < SIZE_THRESHOLD_GB:
        print(
            f"Database size is below threshold ({SIZE_THRESHOLD_GB} GB). No archiving needed."
        )
        return

    print(
        f"Database size exceeds {SIZE_THRESHOLD_GB} GB. Starting archiving process..."
    )

    # Ensure archive directory exists
    ensure_archive_dir()

    # Connect to database (read/write)
    conn = duckdb.connect(DB_PATH)

    try:
        # Archive tables in order to respect foreign key constraints
        # Archive child tables first, then parent tables

        # 1. Archive game_players (joins to games via game_id)
        archive_table(
            conn,
            "game_players",
            join_table="games",
            join_condition="game_players.game_id = games.id",
        )

        # 2. Archive game_teams (joins to games via game_id)
        archive_table(
            conn,
            "game_teams",
            join_table="games",
            join_condition="game_teams.game_id = games.id",
        )

        # 3. Archive player_stats (joins to games via game_id)
        archive_table(
            conn,
            "player_stats",
            join_table="games",
            join_condition="player_stats.game_id = games.id",
        )

        # 4. Archive live_games (assuming joins to games via game_id)
        archive_table(
            conn,
            "live_games",
            join_table="games",
            join_condition="live_games.game_id = games.id",
        )

        # 5. Archive games (direct date column)
        archive_table(conn, "games", date_column="game_date")

        # 6. VACUUM to reclaim space
        print("Running VACUUM to reclaim space...")
        conn.execute("VACUUM")
        conn.commit()

        # Final size check
        final_size_gb = get_db_size_gb()
        print(f"Archiving complete. Final database size: {final_size_gb:.2f} GB")
        print(f"Space saved: {size_gb - final_size_gb:.2f} GB")

    except Exception as e:
        print(f"Error during archiving: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
