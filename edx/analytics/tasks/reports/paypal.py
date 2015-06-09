import datetime
import logging
import luigi
import paypalrestsdk
import json

from edx.analytics.tasks.url import get_target_from_url
from edx.analytics.tasks.url import url_path_join
from edx.analytics.tasks.util.hive import HivePartition
from edx.analytics.tasks.util.overwrite import OverwriteOutputMixin

# Tell urllib3 to switch the ssl backend to PyOpenSSL.
# see https://urllib3.readthedocs.org/en/latest/security.html#pyopenssl
import urllib3.contrib.pyopenssl
urllib3.contrib.pyopenssl.inject_into_urllib3()

log = logging.getLogger(__name__)


class PaypalTaskMixin(OverwriteOutputMixin):

    start_date = luigi.DateParameter(
        default_from_config={'section': 'paypal', 'name': 'start_date'}
    )
    client_mode = luigi.Parameter(
        default_from_config={'section': 'paypal', 'name': 'client_mode'}
    )
    client_id = luigi.Parameter(
        default_from_config={'section': 'paypal', 'name': 'client_id'},
        significant=False,
    )
    # Making this 'insignificant' means it won't be echoed in log files.
    client_secret = luigi.Parameter(
        default_from_config={'section': 'paypal', 'name': 'client_secret'},
        significant=False,
    )
    run_date = luigi.DateParameter(default=datetime.datetime.utcnow().date())


class RawPaypalTransactionLogTask(PaypalTaskMixin, luigi.Task):
    """
    A task that reads out of a remote Paypal account and writes to a file in raw JSON lines format.
    """

    output_root = luigi.Parameter()

    def initialize(self):
        log.debug('Initializing paypalrestsdk')
        paypalrestsdk.configure({
            'mode': self.client_mode,
            'client_id': self.client_id,
            'client_secret': self.client_secret
        })

    def run(self):
        self.initialize()
        self.remove_output_on_overwrite()
        all_payments = []

        end_date = self.run_date + datetime.timedelta(days=1)
        request_args = {
            'start_time': "{}T00:00:00Z".format(self.start_date.isoformat()),  # pylint: disable=no-member
            'end_time': "{}T00:00:00Z".format(end_date.isoformat()),
            'count': 20
        }

        log.debug('Requesting payments')

        # Paypal REST payments API: https://developer.paypal.com/docs/integration/direct/rest-payments-overview/
        # API Reference: https://developer.paypal.com/docs/api/
        payment_history = paypalrestsdk.Payment.all(request_args)
        with self.output().open('w') as output_file:
            if payment_history.payments is None:
                log.error('No payments found')
            else:
                self.write_payments_from_response(payment_history, output_file)

            while payment_history.next_id is not None:
                request_args['start_id'] = payment_history.next_id
                payment_history = paypalrestsdk.Payment.all(request_args)
                self.write_payments_from_response(payment_history, output_file)

    def write_payments_from_response(self, payment_history, output_file):
        log.debug('Received %d payments from paypal', payment_history.count)
        if payment_history.count != len(payment_history.payments):
            log.error('Invalid response, payments do not match count: count=%d, len(payments)=%d', payment_history.count, len(payment_history.payments))

        for payment in payment_history.payments:
            output_file.write(json.dumps(payment.to_dict()))
            output_file.write('\n')

    def requires(self):
        pass

    def output(self):
        url_with_filename = url_path_join(self.output_root, 'paypal_transactions_{0}_{1}.json'.format(self.start_date.isoformat(), self.run_date.isoformat()))
        return get_target_from_url(url_with_filename)


class PaypalTransactionsByDayTask(PaypalTaskMixin, luigi.Task):

    output_root = luigi.Parameter()

    def run(self):
        self.remove_output_on_overwrite()
        if self.overwrite:
            output_dir_target = get_target_from_url(url_path_join(self.output_root, 'daily') + '/')
            output_dir_target.remove()


        output_files = {}

        with self.input()[0].open('r') as input_file:
            for line in input_file:
                payment_record = json.loads(line)
                for trans_with_items in payment_record.get('transactions', []):
                    for transaction in trans_with_items.get('related_resources', []):
                        trans_type = transaction.keys()[0]
                        details = transaction[trans_type]
                        if 'create_time' not in details:
                            log.error('Expected field "create_time" not found in paypal transaction record: %s', line)
                            continue
                        created_date_string = details['create_time'].split('T')[0]

                        output_file = output_files.get(created_date_string)
                        if not output_file:
                            target = get_target_from_url(url_path_join(self.output_root, 'daily', 'dt=' + created_date_string, 'paypal_transactions.json'))
                            output_file = target.open('w')
                            output_files[created_date_string] = output_file

                        record = [
                            created_date_string,
                            payment_record['id'],
                            payment_record['create_time'],
                            payment_record['state'],
                            trans_with_items['invoice_number'],
                            trans_type,
                            details['id'],
                            details['create_time'],
                            details['amount']['total'],
                            details['amount']['currency'],
                            details.get('transaction_fee', {}).get('value', '\\N'),
                            details.get('transaction_fee', {}).get('currency', '\\N'),
                            payment_record['payer']['payer_info']['payer_id'],
                        ]
                        output_file.write('\t'.join(record) + '\n')

        for output_file in output_files.values():
            output_file.close()

        with self.output().open('w') as output_file:
            output_file.write('OK')

    def requires(self):
        yield RawPaypalTransactionLogTask(
            output_root=url_path_join(self.output_root, 'raw')
        )

    def output(self):
        url_with_filename = url_path_join(self.output_root, 'daily', '_SUCCESS')
        return get_target_from_url(url_with_filename)


