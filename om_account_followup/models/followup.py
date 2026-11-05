import logging
from datetime import datetime

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class FollowupFollowup(models.Model):
    _name = 'followup.followup'
    _description = 'Account Follow-up'
    _rec_name = 'name'

    name = fields.Char(string="Name", related='company_id.name', readonly=True)
    followup_line = fields.One2many('followup.line', 'followup_id', 'Follow-up', copy=True)
    company_id = fields.Many2one('res.company', 'Company', required=True, default=lambda self: self.env.company)

    auto_send_email = fields.Boolean(
        'Send Emails Automatically', default=False,
        help="Every day at the sending time, process the follow-ups like "
             "'Send Letters and Emails' does: advance follow-up levels, send "
             "the follow-up emails and assign manual actions. Letters are "
             "not printed automatically.")
    auto_send_time = fields.Float(
        'Sending Time', default=6.0,
        help="Time of day (in the time zone of the 'Send As' user) after "
             "which the automatic follow-up emails are sent.")
    auto_send_user_id = fields.Many2one(
        'res.users', 'Send As',
        default=lambda self: self.env.user if not self.env.su else False,
        domain=[('share', '=', False)],
        help="Automatic follow-ups are processed as this user: the emails "
             "are sent from their address and signature, and the sending "
             "time uses their time zone. Requires accounting rights.")
    auto_send_last_date = fields.Date(
        'Last Automatic Sending', readonly=True, copy=False)

    _sql_constraints = [('company_uniq', 'unique(company_id)',
                         'Only one follow-up per company is allowed')]

    @api.constrains('auto_send_time')
    def _check_auto_send_time(self):
        for rec in self:
            if not 0 <= rec.auto_send_time < 24:
                raise ValidationError(
                    _("The sending time must be between 00:00 and 23:59."))

    @api.model
    def _cron_auto_send_followups(self):
        """Hourly cron: run each enabled follow-up once a day, after its
        sending time."""
        for followup in self.search([('auto_send_email', '=', True)]):
            sender = followup.auto_send_user_id or self.env.ref(
                'base.user_admin', raise_if_not_found=False)
            if not sender:
                continue
            tz_name = sender.tz or followup.company_id.partner_id.tz or 'UTC'
            now = datetime.now(pytz.utc).astimezone(pytz.timezone(tz_name))
            today = now.date()
            if followup.auto_send_last_date and \
                    followup.auto_send_last_date >= today:
                continue
            if now.hour + now.minute / 60.0 < followup.auto_send_time:
                continue
            try:
                with self.env.cr.savepoint():
                    followup._auto_send_followups(sender, today)
                    followup.sudo().auto_send_last_date = today
            except Exception:
                _logger.exception(
                    "Automatic follow-up sending failed for %s",
                    followup.company_id.name)

    def _auto_send_followups(self, sender, date):
        self.ensure_one()
        wizard_model = self.env['followup.print'].with_user(sender).with_company(
            self.company_id).with_context(
            lang=sender.lang, tz=sender.tz, followup_auto=True)
        probe = wizard_model.new({'followup_id': self.id, 'date': date})
        partner_ids = probe._get_partners_followp()['partner_ids']
        if not partner_ids:
            _logger.info("Automatic follow-up for %s: nothing to send",
                         self.company_id.name)
            return
        wizard = wizard_model.create({
            'followup_id': self.id,
            'date': date,
            'partner_ids': [(6, 0, partner_ids)],
        })
        wizard.do_process()
        _logger.info("Automatic follow-up for %s: %s customer(s) processed",
                     self.company_id.name, len(partner_ids))


class FollowupLine(models.Model):
    _name = 'followup.line'
    _description = 'Follow-up Criteria'
    _order = 'delay'

    def _compute_sequence(self):
        delays = [line.delay for line in self.followup_id.followup_line]
        delays.sort()
        for line in self.followup_id.followup_line:
            sequence = delays.index(line.delay)
            line.sequence = sequence+1

    @api.model
    def default_get(self, default_fields):
        values = super(FollowupLine, self).default_get(default_fields)
        if self.env.ref('om_account_followup.email_template_om_account_followup_default'):
            values['email_template_id'] = self.env.ref('om_account_followup.email_template_om_account_followup_default').id
        return values

    name = fields.Char('Follow-Up Action', required=True)
    sequence = fields.Integer('Sequence', compute='_compute_sequence',
                              store=False,
                              help="Gives the sequence order when displaying a list of follow-up lines.")
    followup_id = fields.Many2one('followup.followup', 'Follow Ups',
                                  required=True, ondelete="cascade")
    delay = fields.Integer('Due Days',
                           help="The number of days after the due date of the "
                                "invoice to wait before sending the reminder. Could be negative if you want "
                                "to send a polite alert beforehand.",
                           required=True)
    description = fields.Text('Printed Message', translate=True, default="""
        Dear %(partner_name)s,

Exception made if there was a mistake of ours, it seems that the following
amount stays unpaid. Please, take appropriate measures in order to carry out
this payment in the next 8 days.

Would your payment have been carried out after this mail was sent, please
ignore this message. Do not hesitate to contact our accounting department.

Best Regards,
""", )
    send_email = fields.Boolean('Send an Email', default=True,
                                help="When processing, it will send an email")
    send_letter = fields.Boolean('Send a Letter', default=True,
                                 help="When processing, it will print a letter")
    manual_action = fields.Boolean('Manual Action', default=False,
                                   help="When processing, it will set the "
                                        "manual action to be taken for that customer. ")
    manual_action_note = fields.Text('Action To Do')
    manual_action_responsible_id = fields.Many2one('res.users',
                                                   string='Assign a Responsible', ondelete='set null')
    email_template_id = fields.Many2one('mail.template', 'Email Template',
                                        ondelete='set null')

    _sql_constraints = [('days_uniq', 'unique(followup_id, delay)',
                         'Days of the follow-up levels must be different')]

    @api.constrains('description')
    def _check_description(self):
        for line in self:
            if line.description:
                try:
                    line.description % {'partner_name': '', 'date': '',
                                        'user_signature': '',
                                        'company_name': ''}
                except ValidationError:
                    raise ValidationError(
                        _('Your description is invalid, use the right legend '
                          'or %% if you want to use the percent character.'))
