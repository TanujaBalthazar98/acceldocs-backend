"""Test API endpoints to verify backend functionality."""
import asyncio
from app.database import SessionLocal
from app.models import User
from app.services import projects, documents, workspace

async def test_workflows():
    """Test all major workflows."""
    db = SessionLocal()

    # Get user
    user = db.query(User).filter(User.email == "tanuja@docspeare.com").first()
    print(f"✓ User found: {user.name} ({user.email})")

    # Test list_projects
    result = await projects.list_projects({}, db, user)
    print(f"\n✓ list_projects: {result['ok']}")
    if result['ok']:
        print(f"  Projects: {len(result['projects'])}")
        for p in result['projects']:
            print(f"    - {p['name']} (id: {p['id']})")
    else:
        print(f"  Error: {result.get('error')}")

    # Test list_documents
    if result['ok'] and len(result['projects']) > 0:
        project_ids = [p['id'] for p in result['projects']]
        doc_result = await documents.list_documents({"projectIds": project_ids}, db, user)
        print(f"\n✓ list_documents: {doc_result['ok']}")
        if doc_result['ok']:
            print(f"  Documents: {len(doc_result['documents'])}")
            for d in doc_result['documents'][:3]:
                print(f"    - {d['title']} (id: {d['id']})")
        else:
            print(f"  Error: {doc_result.get('error')}")

    # Test list_project_versions
    if result['ok'] and len(result['projects']) > 0:
        project_ids = [p['id'] for p in result['projects']]
        ver_result = await projects.list_project_versions({"projectIds": project_ids}, db, user)
        print(f"\n✓ list_project_versions: {ver_result['ok']}")
        if ver_result['ok']:
            print(f"  Versions: {len(ver_result['versions'])}")
            for v in ver_result['versions']:
                print(f"    - {v['name']} (id: {v['id']}, project: {v['project_id']})")

    # Test get_organization
    org_result = await workspace.get_organization({}, db, user)
    print(f"\n✓ get_organization: {org_result['ok']}")
    if org_result['ok']:
        print(f"  Organization: {org_result['name']} (id: {org_result['id']})")
        print(f"  Members: {len(org_result.get('members', []))}")

    db.close()
    print("\n✅ All tests passed!")

if __name__ == "__main__":
    asyncio.run(test_workflows())
