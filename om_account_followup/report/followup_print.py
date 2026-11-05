import time
from collections import defaultdict
from markupsafe import Markup, escape
from odoo.exceptions import ValidationError
from odoo import api, fields, models, _
from odoo.tools import format_date


class ReportFollowup(models.AbstractModel):
    _name = 'report.om_account_followup.report_followup'
    _description = 'Report Followup'

    @api.model
    def _get_report_values(self, docids, data=None):
        model = self.env['followup.sending.results']
        ids = self.env.context.get('active_ids') or False
        docs = model.browse(ids)
        return {
            'docs': docs,
            'doc_ids': docids,
            'doc_model': model,
            'time': time,
            'ids_to_objects': self._ids_to_objects,
            'getLines': self._lines_get,
            'get_text': self._get_text,
            'data': data and data['form'] or {}}

    def _ids_to_objects(self, ids):
        all_lines = []
        for line in self.env['followup.stat.by.partner'].browse(ids):
            if line not in all_lines:
                all_lines.append(line)
        return all_lines

    def _lines_get(self, stat_by_partner_line):
        return self._lines_get_with_partner(stat_by_partner_line.partner_id,
                                            stat_by_partner_line.company_id.id)

    def _lines_get_with_partner(self, partner, company_id):
        moveline_obj = self.env['account.move.line']
        moveline_ids = moveline_obj.search(
            moveline_obj._followup_open_domain() +
            [('partner_id', '=', partner.id),
             ('company_id', '=', company_id),
             '|', ('date_maturity', '=', False),
             ('date_maturity', '<=', fields.Date.today())],
            order='date_maturity, date, id')
        lines_per_currency = defaultdict(list)
        totals = defaultdict(float)
        for line in moveline_ids:
            currency = line.currency_id or line.company_id.currency_id
            # amount still due, in the invoice currency
            balance = line.amount_residual
            if currency != line.company_id.currency_id:
                balance = line.amount_residual_currency
            line_data = {
                'name': line.move_id.name,
                'ref': line.ref,
                # 'date': format_date(self.env, line.date),
                # 'date_maturity': format_date(self.env, line.date_maturity),
                'date': line.date,
                'date_maturity': line.date_maturity,
                'balance': balance,
                'currency_id': currency,
            }
            totals[currency] += balance
            lines_per_currency[currency].append(line_data)

        return [{'total': totals[currency], 'line': lines, 'currency': currency}
                for currency, lines in lines_per_currency.items()]

    def _get_text(self, stat_line, followup_id, context=None):
        # letter text in the customer's language, not the printing user's
        lang = stat_line.partner_id.lang or self.env.lang
        self = self.with_context(lang=lang)
        fp_obj = self.env['followup.followup']
        fp_line = fp_obj.browse(followup_id).followup_line
        if not fp_line:
            raise ValidationError(
                _("The followup plan defined for the current company does not "
                  "have any followup action."))
        default_text = ''
        li_delay = []
        for line in fp_line:
            if not default_text and line.description:
                default_text = line.description
            li_delay.append(line.delay)
        li_delay.sort(reverse=True)
        aml_obj = self.env['account.move.line']
        partner_line_ids = aml_obj.search(
            aml_obj._followup_open_domain() +
            [('partner_id', '=', stat_line.partner_id.id),
             ('company_id', '=', stat_line.company_id.id),
             ('followup_line_id', '!=', False)])

        partner_max_delay = 0
        partner_max_text = ''
        for i in partner_line_ids:
            if i.followup_line_id.delay > partner_max_delay and \
                    i.followup_line_id.description:
                partner_max_delay = i.followup_line_id.delay
                partner_max_text = i.followup_line_id.with_context(
                    lang=lang).description
        text = partner_max_delay and partner_max_text or default_text
        if text:
            text = text.strip() % {
                'partner_name': stat_line.partner_id.name,
                'date': format_date(self.env, fields.Date.context_today(self),
                                    lang_code=lang),
                'company_name': stat_line.company_id.name,
                'user_signature': self.env.user.signature or '',
            }
            text = Markup('<br/>').join(
                escape(t.strip()) for t in text.split('\n'))
        return text
