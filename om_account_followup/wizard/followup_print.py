import datetime
import time
from odoo import api, fields, models, _
from markupsafe import Markup, escape

from ..models.account_move import FOLLOWUP_ACTIVE_SQL


class FollowupPrint(models.TransientModel):
    _name = 'followup.print'
    _description = 'Print Follow-up & Send Mail to Customers'

    def _get_followup(self):
        if self.env.context.get('active_model',
                                'ir.ui.menu') == 'followup.followup':
            return self.env.context.get('active_id', False)
        company_id = self.env.user.company_id.id
        followp_id = self.env['followup.followup'].search(
            [('company_id', '=', company_id)], limit=1)
        return followp_id or False

    date = fields.Date('Follow-up Sending Date', required=True,
                       help="This field allow you to select a forecast date "
                            "to plan your follow-ups",
                       default=lambda *a: time.strftime('%Y-%m-%d'))
    followup_id = fields.Many2one('followup.followup', 'Follow-Up',
                                  required=True, readonly=True,
                                  default=_get_followup)
    partner_ids = fields.Many2many('followup.stat.by.partner',
                                   'partner_stat_rel', 'osv_memory_id',
                                   'partner_id', 'Partners', required=True)
    company_id = fields.Many2one('res.company', readonly=True,
                                 related='followup_id.company_id')
    email_conf = fields.Boolean('Send Email Confirmation')
    email_subject = fields.Char('Email Subject', size=64,
                                default=lambda self: self.env._('Invoices Reminder'))
    partner_lang = fields.Boolean(
        'Send Email in Partner Language', default=True,
        help='Do not change message text, if you want to send email in '
             'partner language, or configure from company')
    email_body = fields.Text('Email Body', default='')
    summary = fields.Text('Summary', readonly=True)
    test_print = fields.Boolean(
        'Test Print', help='Check if you want to print follow-ups without '
                           'changing follow-up level.')

    def process_partners(self, partner_ids, data):
        partner_obj = self.env['res.partner']
        partner_ids_to_print = []
        nbmanuals = 0
        manuals = {}
        nbmails = 0
        nbunknownmails = 0
        nbprints = 0
        resulttext = " "
        for partner in self.env['followup.stat.by.partner'].browse(
                partner_ids):
            if partner.max_followup_id.manual_action:
                partner_obj.do_partner_manual_action([partner.partner_id.id])
                nbmanuals = nbmanuals + 1
                key = partner.partner_id.payment_responsible_id.name or _(
                    "Anybody")
                if key not in manuals.keys():
                    manuals[key] = 1
                else:
                    manuals[key] = manuals[key] + 1
            if partner.max_followup_id.send_email:
                nbunknownmails += partner.partner_id.do_partner_mail()
                nbmails += 1
            # letters cannot be printed by the automatic (cron) sending
            if partner.max_followup_id.send_letter and \
                    not self.env.context.get('followup_auto'):
                partner_ids_to_print.append(partner.id)
                nbprints += 1
                followup_without_lit = \
                    partner.partner_id.latest_followup_level_id_without_lit
                message_html = Markup(_(
                    "Follow-up letter of <i>{followup}</i> will be sent"
                )).format(
                    followup=escape(followup_without_lit.name)
                )

                partner.partner_id.message_post(
                    body=message_html,
                    message_type='comment'
                )
        if nbunknownmails == 0:
            resulttext += str(nbmails) + _(" email(s) sent")
        else:
            resulttext += str(nbmails) + _(
                " email(s) should have been sent, but ") + str(
                nbunknownmails) + _(
                " had unknown email address(es)") + "\n <BR/> "
        resulttext += "<BR/>" + str(nbprints) + _(
            " letter(s) in report") + " \n <BR/>" + str(nbmanuals) + _(
            " manual action(s) assigned:")
        needprinting = False
        if nbprints > 0:
            needprinting = True
        resulttext = Markup(resulttext)
        resulttext += Markup("<p align='center'>")
        for item, count in manuals.items():
            resulttext += Markup("<li>%s:%s</li>") % (escape(item), count)
        resulttext += Markup("</p>")
        result = {}
        action = partner_obj.do_partner_print(partner_ids_to_print, data)
        result['needprinting'] = needprinting
        result['resulttext'] = resulttext
        result['action'] = action or {}
        return result

    def do_update_followup_level(self, to_update, partner_list, date):
        for id in to_update.keys():
            if to_update[id]['partner_id'] in partner_list:
                self.env['account.move.line'].browse([int(id)]).write(
                    {'followup_line_id': to_update[id]['level'],
                     'followup_date': date})

    def clear_manual_actions(self, partner_list):
        partner_list_ids = [partner.partner_id.id for partner in self.env[
            'followup.stat.by.partner'].browse(partner_list)]
        ids = self.env['res.partner'].search(
            ['&', ('id', 'not in', partner_list_ids), '|',
             ('payment_responsible_id', '!=', False),
             ('payment_next_action_date', '!=', False)])

        partners_to_clear = []
        for part in ids:
            if not part.unreconciled_aml_ids.filtered(lambda l: not l.blocked):
                partners_to_clear.append(part.id)
                part.action_done()
        return len(partners_to_clear)

    def do_process(self):
        context = dict(self.env.context or {})

        tmp = self._get_partners_followp()
        partner_list = tmp['partner_ids']
        to_update = tmp['to_update']
        date = self.date
        data = self.read()[0]
        data['followup_id'] = data['followup_id'][0]

        self.do_update_followup_level(to_update, partner_list, date)
        # process_partners reads each customer's level from the
        # followup.stat.by.partner SQL view: write the new levels to the
        # database and drop cached view rows first, otherwise a customer
        # reaching its first level gets no email and the others get the
        # email of their previous level
        self.env['account.move.line'].flush_model(
            ['followup_line_id', 'followup_date'])
        self.env['followup.stat.by.partner'].invalidate_model()
        self.env['res.partner'].invalidate_model([
            'latest_followup_level_id', 'latest_followup_level_id_without_lit',
            'latest_followup_date'])
        restot_context = context.copy()
        restot = self.with_context(restot_context).process_partners(
            partner_list, data)
        context.update(restot_context)
        nbactionscleared = self.clear_manual_actions(partner_list)
        if nbactionscleared > 0:
            restot['resulttext'] = restot['resulttext'] + "<li>" + _(
                "%s partners have no credits and as such the "
                "action is cleared") % (str(nbactionscleared)) + "</li>"
        resource_id = self.env.ref(
            'om_account_followup.view_om_account_followup_sending_results')
        context.update({'description': restot['resulttext'],
                        'needprinting': restot['needprinting'],
                        'report_data': restot['action']})
        return {
            'name': _('Send Letters and Emails: Actions Summary'),
            'view_type': 'form',
            'context': context,
            'view_mode': 'list,form',
            'res_model': 'followup.sending.results',
            'views': [(resource_id.id, 'form')],
            'type': 'ir.actions.act_window',
            'target': 'new',
        }

    def _get_msg(self):
        return self.env.user.company_id.follow_up_msg

    def _get_partners_followp(self):
        data = self
        company_id = data.company_id.id
        context = self.env.context
        # raw SQL below: write pending ORM changes first
        self.env['account.move.line'].flush_model()
        self.env['followup.line'].flush_model()
        self._cr.execute(
            '''SELECT
                    l.partner_id,
                    l.followup_line_id,
                    l.date_maturity,
                    l.date, l.id, l.followup_date
                FROM account_move_line AS l
                LEFT JOIN account_account AS a
                ON (l.account_id=a.id)
                WHERE ''' + FOLLOWUP_ACTIVE_SQL + '''
                AND (l.partner_id is NOT NULL)
                AND (l.company_id = %s)
                ORDER BY l.date''',
            (company_id,)
        )
        move_lines = self._cr.fetchall()
        fup_id = 'followup_id' in context and context[
            'followup_id'] or data.followup_id.id
        date = 'date' in context and context['date'] or data.date
        date = fields.Date.to_string(date)
        current_date = datetime.date(*time.strptime(date, '%Y-%m-%d')[:3])
        fup_id = int(fup_id)
        self.env.cr.execute(
            """
            SELECT *
            FROM followup_line
            WHERE followup_id = %s
            ORDER BY delay
            """,
            (fup_id,)
        )

        # levels of this plan, by increasing delay
        level_rows = self._cr.dictfetchall()
        levels = [(r['delay'], r['id']) for r in level_rows]
        level_delay = {lid: d for d, lid in levels}
        level_repeat = {r['id']: r.get('repeat_interval') or 0
                        for r in level_rows}

        partner_list = []
        to_update = {}

        for partner_id, followup_line_id, date_maturity, date, id, \
                followup_date in move_lines:
            if not partner_id:
                continue
            if followup_line_id and followup_line_id not in level_delay:
                continue  # level of another follow-up plan
            ref_date = date_maturity or date
            if not ref_date:
                continue
            overdue_days = (current_date - ref_date).days
            # jump straight to the highest level reached, so an invoice that
            # is already very overdue gets one email (of its real level)
            # instead of one email per level on consecutive runs
            reached = [lid for d, lid in levels if d <= overdue_days]
            if not reached:
                continue
            target = reached[-1]
            if followup_line_id and \
                    level_delay[target] <= level_delay[followup_line_id]:
                # no higher level reached: repeat the current level if it is
                # set to repeat and the interval has passed since last time
                repeat = level_repeat.get(followup_line_id)
                if not (repeat and followup_date and
                        (current_date - followup_date).days >= repeat):
                    continue
                target = followup_line_id
            stat_line_id = partner_id * 10000 + company_id
            if stat_line_id not in partner_list:
                partner_list.append(stat_line_id)
            to_update[str(id)] = {'level': target,
                                  'partner_id': stat_line_id}
        return {'partner_ids': partner_list, 'to_update': to_update}
