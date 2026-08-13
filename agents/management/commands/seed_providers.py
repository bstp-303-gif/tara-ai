from django.core.management.base import BaseCommand
from agents.models import CertificationRule, Provider

SEED_DATA = [
    {
        'name': 'Microsoft',
        'display_name': 'Microsoft',
        'rules': [
            {
                'canonical_name': 'Microsoft Certified Educator (MCE)',
                'required_keywords': ['certified educator'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 10,
            },
            {
                'canonical_name': 'Microsoft Innovative Educator Expert (MIEE)',
                'required_keywords': ['innovative educator', 'expert'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 5,
            },
            {
                'canonical_name': 'Microsoft Innovative Educator Expert (MIEE)',
                'required_keywords': ['expert'],
                'exclude_keywords': ['certified educator', 'innovative educator'],
                'is_eligible': True,
                'priority': 4,
            },
        ],
    },
    {
        'name': 'Google',
        'display_name': 'Google',
        'rules': [
            {
                'canonical_name': 'Google Certified Trainer (GCT)',
                'required_keywords': ['trainer'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 10,
            },
            {
                'canonical_name': 'Google Certified Coach (GCC)',
                'required_keywords': ['coach'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 9,
            },
            {
                'canonical_name': 'Google Certified Educator Level 2 (GCE L2)',
                'required_keywords': ['level 2'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 8,
            },
            {
                'canonical_name': 'Google Certified Educator Level 2 (GCE L2)',
                'required_keywords': ['l2'],
                'exclude_keywords': ['level 1', 'l1'],
                'is_eligible': True,
                'priority': 7,
            },
        ],
    },
    {
        'name': 'Apple',
        'display_name': 'Apple',
        'rules': [
            {
                'canonical_name': 'Apple Learning Coach (ALC)',
                'required_keywords': ['learning coach'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 10,
            },
            {
                'canonical_name': 'Apple Teacher',
                'required_keywords': ['apple teacher'],
                'exclude_keywords': [],
                'is_eligible': True,
                'priority': 5,
            },
            {
                'canonical_name': 'Apple Teacher',
                'required_keywords': ['teacher'],
                'exclude_keywords': ['learning coach'],
                'is_eligible': True,
                'priority': 4,
            },
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed Provider and CertificationRule data. Safe to re-run (skips existing).'

    def handle(self, *args, **options):
        for p_data in SEED_DATA:
            provider, created = Provider.objects.get_or_create(
                name=p_data['name'],
                defaults={'display_name': p_data['display_name'], 'is_active': True},
            )
            if created:
                self.stdout.write(f'  Created provider: {provider.name}')
            else:
                self.stdout.write(f'  Provider exists: {provider.name}')

            for r in p_data['rules']:
                rule, r_created = CertificationRule.objects.get_or_create(
                    provider=provider,
                    canonical_name=r['canonical_name'],
                    required_keywords=r['required_keywords'],
                    defaults={
                        'exclude_keywords': r['exclude_keywords'],
                        'is_eligible': r['is_eligible'],
                        'priority': r['priority'],
                    },
                )
                if r_created:
                    self.stdout.write(f'    + Rule: {rule.canonical_name}')

        self.stdout.write(self.style.SUCCESS('Done.'))
