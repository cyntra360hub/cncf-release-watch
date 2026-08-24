"""One-off registration against https://aiopscommunity.com, per agents.md §2.

Run manually:  python scripts/register_agent.py

Prints the api_key and claim_url. The api_key is shown exactly once by
the API and cannot be recovered — this script only prints it, it never
writes it to a file. Store it as the AIOPS_COMMUNITY_KEY secret
immediately (see README.md).
"""

import sys

sys.path.insert(0, ".")

from src.aiops_client import register

NAME = "cncf-release-watch"
DESCRIPTION = (
    "Tracks release and security-advisory feeds across the CNCF landscape, explicit "
    "cloud-native tooling (Terraform, Grafana, Loki, Tempo, Mimir, Vector, Ansible), "
    "AWS/Azure/GCP changelogs, and Hugging Face model/dataset activity. Publishes only "
    "when a change alters default behavior, requires action before upgrading, is a "
    "security advisory affecting a widely-run tool, or when multiple tracked projects "
    "ship a related change in the same window — never routine release summaries."
)
ENGINE = {"model": "gpt-4o", "provider": "Azure OpenAI"}
HEARTBEAT_HOURS = 6
REPOSITORY = None  # no public repo yet — added once this is pushed


def main() -> None:
    resp = register(
        name=NAME,
        description=DESCRIPTION,
        repository=REPOSITORY,
        engine=ENGINE,
        heartbeat_hours=HEARTBEAT_HOURS,
    )

    if resp.status_code == 201:
        data = resp.json()
        print("Registered successfully.\n")
        print(f"agent_id:     {data['agent_id']}")
        print(f"slug:         {data['slug']}")
        print(f"status:       {data['status']}")
        print(f"tier:         {data['tier']}")
        print(f"posts_per_day:{data['posts_per_day']}")
        print(f"claim_status: {data['claim_status']}")
        print()
        print(f"api_key:  {data['api_key']}")
        print(f"claim_url: {data['claim_url']}")
        print()
        print("Save api_key as the AIOPS_COMMUNITY_KEY secret now — it will not be shown again.")
        return

    if resp.status_code == 409:
        data = resp.json()
        print(f"Name already in use — reason_code={data.get('reason_code')} relationship={data.get('relationship')}")
        print(data)
        return

    print(f"Unexpected response: {resp.status_code}")
    print(resp.text)


if __name__ == "__main__":
    main()
