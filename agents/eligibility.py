import pandas as pd
from django.db import transaction
from .models import CertificationRule, Teacher


def classify_certification(provider, programme, level):
    """
    Match a raw (provider, programme, level) record against CertificationRule
    rows in the database.  Returns the canonical_name of the first matching
    eligible rule, or None.
    """
    p = str(provider or '').strip().lower()
    rules = (
        CertificationRule.objects
        .filter(provider__name__iexact=p, provider__is_active=True, is_eligible=True)
        .order_by('-priority', 'canonical_name')
        .select_related('provider')
    )
    for rule in rules:
        if rule.matches(programme, level):
            return rule.canonical_name
    return None


def normalize_and_deduplicate(dataframes_dict, providers_list):
    """
    Takes multiple dataframes and normalizes + deduplicates them.

    Eligibility rule: a teacher is Eligible if they hold one or more of the
    approved certifications in RECOGNIZED_CERTIFICATIONS (across any provider).

    Returns: (combined_df, deduplicated_df, eligible_df, summary_stats)
    """
    try:
        combined_df = pd.concat(dataframes_dict.values(), ignore_index=True)
        if 'certification' not in combined_df.columns:
            combined_df['certification'] = ''

        # Deduplicate by IC number, collecting every provider and every
        # recognised certification a teacher holds.
        seen_ics = {}

        for idx, row in combined_df.iterrows():
            ic = str(row['ic']).strip()
            recognized = classify_certification(
                row.get('provider'), row.get('certification'), row.get('cert_level')
            )

            if ic not in seen_ics:
                row_dict = row.to_dict()
                row_dict['multi_certified'] = False
                row_dict['provider_list'] = [row['provider']]
                row_dict['recognized_list'] = [recognized] if recognized else []
                seen_ics[ic] = row_dict
            else:
                entry = seen_ics[ic]
                if row['provider'] not in entry['provider_list']:
                    entry['provider_list'].append(row['provider'])
                    entry['multi_certified'] = True
                if recognized and recognized not in entry['recognized_list']:
                    entry['recognized_list'].append(recognized)

        dedup_data = []
        for ic, row_dict in seen_ics.items():
            row_dict = dict(row_dict)
            row_dict['provider'] = ', '.join(sorted(set(row_dict['provider_list'])))
            recognized = sorted(set(row_dict['recognized_list']))
            row_dict['certification'] = ', '.join(recognized)
            row_dict['eligibility_status'] = 'Eligible' if recognized else 'Not Eligible'
            del row_dict['provider_list']
            del row_dict['recognized_list']
            dedup_data.append(row_dict)

        deduplicated_df = pd.DataFrame(dedup_data)
        deduplicated_df['cert_year'] = pd.to_numeric(deduplicated_df['cert_year'], errors='coerce')

        # Create eligible dataframe with rank
        eligible_df = deduplicated_df[deduplicated_df['eligibility_status'] == 'Eligible'].copy()
        eligible_df = eligible_df.sort_values(['state', 'name']).reset_index(drop=True)
        eligible_df['rank'] = range(1, len(eligible_df) + 1)

        stats = {
            'total_records': len(combined_df),
            'total_unique': len(deduplicated_df),
            'multi_certified': int(deduplicated_df['multi_certified'].sum()),
            'eligible': len(eligible_df),
            'not_eligible': len(deduplicated_df[deduplicated_df['eligibility_status'] == 'Not Eligible'])
        }

        return combined_df, deduplicated_df, eligible_df, stats

    except Exception as e:
        print(f"Error in normalization/deduplication: {str(e)}")
        return None, None, None, {}


def save_to_database(deduplicated_df):
    """
    Save deduplicated teachers to Teacher model.
    """
    try:
        with transaction.atomic():
            Teacher.objects.all().delete()

            for idx, row in deduplicated_df.iterrows():
                Teacher.objects.create(
                    ic_number=str(row['ic']).strip(),
                    full_name=str(row['name']).strip(),
                    email=str(row['email']).strip(),
                    school=str(row['school']).strip(),
                    state=str(row['state']).strip(),
                    provider=row['provider'],
                    certification=str(row.get('certification', '')).strip(),
                    cert_level=str(row['cert_level']).strip(),
                    cert_year=int(row['cert_year']) if pd.notna(row['cert_year']) else 0,
                    multi_certified=bool(row['multi_certified']),
                    eligibility_status=row['eligibility_status']
                )
        return True
    except Exception as e:
        print(f"Error saving to database: {str(e)}")
        return False


def save_dataframe_to_excel(dataframe, output_path, sheet_name='Data'):
    """
    Save dataframe to Excel with formatting.
    """
    try:
        from openpyxl import load_workbook

        dataframe.to_excel(output_path, index=False, sheet_name=sheet_name)

        wb = load_workbook(output_path)
        ws = wb.active

        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except Exception:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width

        wb.save(output_path)
        return True
    except Exception as e:
        print(f"Error saving dataframe to Excel: {str(e)}")
        return False
