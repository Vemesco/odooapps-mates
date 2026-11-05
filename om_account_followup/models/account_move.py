from odoo import api, fields, models, _

# A receivable line is "open for follow-up" when it belongs to a posted entry,
# is not reconciled and still has an amount due (positive residual). Fully or
# partially matched payments/credit notes therefore never show up, and invoices
# show what is still owed, not their original amount.
# The amount must also be due in the invoice currency: foreign-currency
# invoices paid in full but with a leftover company-currency exchange
# difference (never booked) are not owed by the customer.
FOLLOWUP_MIN_RESIDUAL = 0.005
FOLLOWUP_OPEN_DOMAIN = [
    ('account_id.account_type', '=', 'asset_receivable'),
    ('parent_state', '=', 'posted'),
    ('reconciled', '=', False),
    ('amount_residual', '>', 0),
    ('amount_residual_currency', '>', FOLLOWUP_MIN_RESIDUAL),
]
# Same criteria for raw SQL; expects account_move_line aliased "l" and
# account_account aliased "a".
FOLLOWUP_OPEN_SQL = """
    a.account_type = 'asset_receivable'
    AND l.parent_state = 'posted'
    AND l.reconciled IS NOT TRUE
    AND l.amount_residual > 0
    AND l.amount_residual_currency > %s
""" % FOLLOWUP_MIN_RESIDUAL
# Lines flagged "No Follow-up" are excluded from levels, emails, letters,
# amounts and statistics.
FOLLOWUP_ACTIVE_SQL = FOLLOWUP_OPEN_SQL + " AND l.blocked IS NOT TRUE "


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    followup_line_id = fields.Many2one('followup.line', 'Follow-up Level')
    followup_date = fields.Date('Latest Follow-up')
    result = fields.Float(compute='_get_result', string="Balance Amount")
    blocked = fields.Boolean(
        'No Follow-up', copy=False,
        help="Check this box to exclude this journal item from payment "
             "follow-ups (reminder emails, letters and amounts due), e.g. "
             "because it is in litigation or a payment agreement exists.")

    @api.model
    def _followup_open_domain(self, include_blocked=False):
        domain = list(FOLLOWUP_OPEN_DOMAIN)
        if not include_blocked:
            domain.append(('blocked', '=', False))
        return domain

    @api.depends('amount_residual')
    def _get_result(self):
        for aml in self:
            aml.result = aml.amount_residual


class AccountMove(models.Model):
    _inherit = 'account.move'

    followup_blocked = fields.Boolean(
        'No Follow-up', compute='_compute_followup_blocked',
        inverse='_inverse_followup_blocked',
        search='_search_followup_blocked',
        help="Exclude this entry's receivable items from payment follow-ups "
             "(reminder emails, letters and amounts due).")

    def _followup_receivable_lines(self):
        return self.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable')

    @api.depends('line_ids.blocked', 'line_ids.account_id')
    def _compute_followup_blocked(self):
        for move in self:
            lines = move._followup_receivable_lines()
            move.followup_blocked = bool(lines) and all(lines.mapped('blocked'))

    def _inverse_followup_blocked(self):
        for move in self:
            move._followup_receivable_lines().write(
                {'blocked': move.followup_blocked})

    def _search_followup_blocked(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise NotImplementedError(
                _("Unsupported search on No Follow-up: %s %s", operator, value))
        positive = (operator == '=') == value
        domain = [('line_ids', 'any', [
            ('account_id.account_type', '=', 'asset_receivable'),
            ('blocked', '=', True)])]
        return domain if positive else ['!'] + domain
