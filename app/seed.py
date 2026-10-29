"""Create a login user for local development (idempotent).

    python -m app.seed --phone 9360306991 --role manager

Login OTPs are per role, not per user: MANAGER_OTP / EXECUTOR_OTP in .env.
A manager is anyone with an active CAMPAIGN_MANAGER membership, so the manager
role also gets a placeholder campaign to hold that membership.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from app.auth import get_user_by_phone, resolve_user_role
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import Campaign, CampaignMember, CampaignRole, Organization, User

ORG_NAME = "Experientia Dev"
SETUP_CAMPAIGN = "Manager setup"


def seed(phone: str, role: str, first_name: str) -> None:
    settings = get_settings()
    if settings.is_sqlite:
        # Prisma owns the Postgres schema; only the local SQLite file needs tables.
        Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        user = get_user_by_phone(db, phone)
        if user:
            print(f"[experientia] user {phone} already exists ({user.id})")
        else:
            org = db.query(Organization).filter(Organization.isActive.is_(True)).first()
            if not org:
                org = Organization(name=ORG_NAME, type="AGENCY")
                db.add(org)
                db.flush()
                print(f"[experientia] created organization {org.name} ({org.id})")
            user = User(organizationId=org.id, firstName=first_name, lastName="", phone=phone)
            db.add(user)
            db.flush()
            print(f"[experientia] created user {phone} ({user.id})")

        if role == "manager" and resolve_user_role(db, user) != "manager":
            campaign = Campaign(
                organizationId=user.organizationId,
                name=SETUP_CAMPAIGN,
                description="Holds the manager membership for a seeded user.",
                status="ACTIVE",
                startDate=datetime.utcnow(),
            )
            db.add(campaign)
            db.flush()
            db.add(
                CampaignMember(
                    campaignId=campaign.id,
                    userId=user.id,
                    assignedBy=user.id,
                    role=CampaignRole.CAMPAIGN_MANAGER,
                )
            )
            print(f"[experientia] made {phone} a campaign manager")

        db.commit()
        final_role = resolve_user_role(db, user)
        otp = settings.manager_otp if final_role == "manager" else settings.executor_otp
        print(f"[experientia] login: phone={phone} otp={otp} role={final_role}")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a login user.")
    parser.add_argument("--phone", required=True)
    parser.add_argument("--role", choices=["manager", "executor"], default="manager")
    parser.add_argument("--first-name", default="Dev")
    args = parser.parse_args()
    seed(args.phone, args.role, args.first_name)
