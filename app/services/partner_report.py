"""Private partner accounting view. Existing credit commission is not cash income."""
from sqlalchemy import select,func
from app.models import Group,User,Payment,CreditTx
from app.services import boards,settings


async def summary(s, partner_id: int) -> dict:
    groups=(await s.scalars(select(Group).where(Group.board_admin==partner_id))).all()
    tags=[boards.source_tag(g.id) for g in groups]
    visitors=await s.scalar(select(func.count()).select_from(User).where(User.source.in_(tags))) if tags else 0
    # Use the commission recipient recorded at payment time, never another
    # owner's current group/source attribution. DISTINCT prevents duplicate refs
    # from multiplying the payment amount in the reporting view.
    payments=(await s.scalars(select(Payment).join(
        CreditTx,CreditTx.ref==('pay-'+Payment.charge_id)).where(
        CreditTx.user_id==partner_id,CreditTx.kind=='board',Payment.days>=0,
        Payment.currency=='RUB').distinct())).all()
    gross=sum(p.amount for p in payments)
    refunds=sum(p.amount if p.refunded else min(p.amount,max(0,p.refunded_amount)) for p in payments)
    accrued=await s.scalar(select(func.coalesce(func.sum(CreditTx.amount),0)).where(
        CreditTx.user_id==partner_id,CreditTx.kind=='board')) or 0
    reversed_credits = await s.scalar(select(func.coalesce(func.sum(CreditTx.amount),0)).where(
        CreditTx.user_id==partner_id,CreditTx.kind=="reverse",CreditTx.ref.startswith("rev-"))) or 0
    user=await s.get(User,partner_id)
    return dict(groups=len(groups),visitors=visitors or 0,payments=len(payments),
                subscription_payments=sum(p.days > 0 for p in payments),
                ad_payments=sum(p.days == 0 for p in payments),gross_kop=gross,refunds_kop=refunds,net_kop=gross-refunds,
                commission_credits=accrued,reversed_credits=-reversed_credits,available_credits=user.credits if user else 0,
                held_credits=user.credits_hold if user else 0,
                percent=await settings.get_int(s,'board_percent'))
