"""
Management command to backfille history.
"""
import logging
import os
import time
from django.core.management.base import BaseCommand
from django.db import connection, transaction

log = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Backfille history for models using django-simple-history.
    Example usage:
    $ ./manage.py lms backfill_history --batchsize 1000 --sleep_between 1 --settings=devstack
    """

    help = (
        "Populates the historical records with an initial state as of 06/29"
    )

    # Size in bytes to read from input file per batch
    DEFAULT_SIZE = 1000000
    DEFAULT_SLEEP_BETWEEN_INSERTS = 1
    DATE = '2019-06-29'
    HISTORY_USER_ID = None
    HISTORY_CHANGE_REASON = 'initial history population'

    TABLES = [
        #{'name': 'organizations_organization', 'exclude_column': None, 'input_filename': 'organizations_organization_2019_06_29.csv'},
        {'name': 'entitlements_courseentitlement', 'exclude_column': None, 'input_filename': 'entitlements_courseentitlement_2019_06_29.csv'}
    ]

    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)

        parser.add_argument(
            '--sleep_between',
            default=self.DEFAULT_SLEEP_BETWEEN_INSERTS,
            type=float,
            help='Seconds to sleep between chunked inserts.'
        )

        parser.add_argument(
            "--size",
            action="store",
            default=self.DEFAULT_SIZE,
            type=int,
            help="Maximum number of bytes to read from input file in each batch.",
        )

        parser.add_argument(
            "--input_root",
            action="store",
        )

    def handle(self, *args, **options):
        byte_size = options['size']
        sleep_between = options['sleep_between']
        input_root = options['input_root']

        for table_info in self.TABLES:
            table = table_info['name']
            historical_table = "_historical".join(table.rsplit('_', 1))
            exclude_column = table_info['exclude_column']
            input_filename = table_info['input_filename']
            file_path = os.path.join(input_root, input_filename)

            with connection.cursor() as cursor:
                query = u"""
                    SELECT
                        column_name
                    FROM information_schema.columns
                    WHERE table_name='{}'
                    ORDER BY ordinal_position
                    """.format(table)
                cursor.execute(query)
                columns = [column[0] for column in cursor.fetchall()]
            if exclude_column in columns:
                columns.remove(exclude_column)
            columns = ['`{}`'.format(c) for c in columns]

            with open(file_path) as input_file:
                # Skip the header
                input_file.readline()
                while True:
                    lines = input_file.readlines(byte_size)
                    if not lines:
                        break

                    values = [line.strip('\n').split(',') for line in lines]
                    # Add history columns data
                    [value.extend([self.DATE, self.HISTORY_CHANGE_REASON, '+', self.HISTORY_USER_ID]) for value in values]
                    # Convert to tuple
                    values = [tuple(value) for value in values]
                    # Get the ids
                    ids = [value[0] for value in values]

                    # Checks for existing historical records
                    with connection.cursor() as cursor:
                        query = u"""
                            SELECT COUNT(1)
                            FROM {historical_table}
                            WHERE ID in ({ids})
                            AND history_type='+'
                            """.format(
                                historical_table=historical_table,
                                ids=','.join(ids)
                            )
                        log.info(query)
                        cursor.execute(query)
                        count = cursor.fetchone()[0]

                    if count==len(ids):
                        log.info(u"Initial history records already exist for ids %s..%s - skipping.", ','.join(ids[:2]), ','.join(ids[-2:]))
                        continue
                    elif count!=0:
                        raise Exception(u"Database count: %s does not match input count: %s" % (count,len(ids)))

                    with transaction.atomic():
                        with connection.cursor() as cursor:
                            query = u"""
                                INSERT INTO {historical_table}(
                                    {insert_columns},history_date,history_change_reason,history_type,history_user_id
                                )
                                VALUES ({placeholder})
                                """.format(
                                    historical_table=historical_table,
                                    insert_columns=','.join(columns),
                                    placeholder=','.join(['%s']*(len(columns)+4))
                                )
                            log.info(query)
                            cursor.executemany(query, values)

                    log.info("Sleeping %s seconds...", sleep_between)
                    time.sleep(sleep_between)
