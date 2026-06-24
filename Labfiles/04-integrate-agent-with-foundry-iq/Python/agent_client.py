import os
import platform
from dotenv import load_dotenv

# Work around Windows WMI calls triggered by platform.platform() during azure.identity import.
# Some environments hang or are very slow in this path.
platform.platform = lambda *args, **kwargs: "Windows"

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# Load environment variables
load_dotenv()
project_endpoint = os.getenv("PROJECT_ENDPOINT")
agent_name = os.getenv("AGENT_NAME")

# Validate configuration
if not project_endpoint or not agent_name:
    raise ValueError("PROJECT_ENDPOINT and AGENT_NAME must be set in .env file")

print(f"Connecting to project: {project_endpoint}")
print(f"Using agent: {agent_name}\n")

# TODO: Connect to the project and create a conversation
# Add your code here to:
# 1. Create DefaultAzureCredential
# 2. Create AIProjectClient with endpoint
# 3. Get the OpenAI client
# 4. Get the agent by name
# 5. Create a new conversation

 # Connect to the project and agent
credential = DefaultAzureCredential(
     exclude_environment_credential=True,
     exclude_managed_identity_credential=True
 )
project_client = AIProjectClient(
     credential=credential,
     endpoint=project_endpoint
 )

 # Get the OpenAI client
openai_client = project_client.get_openai_client()

 # Get the agent
agent = project_client.agents.get(agent_name=agent_name)
print(f"Connected to agent: {agent.name} (id: {agent.id})\n")

# Resolve latest agent version details and inspect tool settings.
latest_version = agent.versions.latest if hasattr(agent, "versions") and agent.versions else None
agent_version_id = latest_version.id if latest_version and hasattr(latest_version, "id") else agent.id
agent_definition = latest_version.definition if latest_version and hasattr(latest_version, "definition") else None
agent_tools = agent_definition.tools if agent_definition and hasattr(agent_definition, "tools") else None

print(f"Using agent version id: {agent_version_id}")
if agent_tools:
    print("Agent tools and approval settings:")
    for tool in agent_tools:
        tool_type = tool.get("type") if isinstance(tool, dict) else getattr(tool, "type", "unknown")
        require_approval = tool.get("require_approval") if isinstance(tool, dict) else getattr(tool, "require_approval", None)
        print(f"  - type={tool_type}, require_approval={require_approval}")
else:
    print("Agent tools were not present in the fetched agent definition.")
print("")

# web_search_preview does not expose require_approval in this SDK, so enforce approval client-side.
HAS_WEB_SEARCH_TOOL = any(
    (tool.get("type") if isinstance(tool, dict) else getattr(tool, "type", None))
    in ("web_search_preview", "web_search_preview_2025_03_11", "web_search", "web_search_2025_08_26")
    for tool in (agent_tools or [])
)

 # Create a new conversation
conversation = openai_client.conversations.create(items=[])
print(f"Created conversation (id: {conversation.id})\n")


# Conversation history for context (client-side tracking)
conversation_history = []


def send_message_to_agent(user_message):
    """
    Send a message to the agent and handle the response using the conversations API.
    """
    try:
        print("\nAgent: ", end="", flush=True)
        
        # Add user message to the conversation
        openai_client.conversations.items.create(
            conversation_id=conversation.id,
            items=[{"type": "message", "role": "user", "content": user_message}],
        )

        # Store in conversation history (client-side)
        conversation_history.append({
            "role": "user",
            "content": user_message
        })

        # Enforce per-turn approval for web search tools.
        tool_choice_override = None
        if HAS_WEB_SEARCH_TOOL:
            print("[Approval required for web search tool usage this turn]")
            approval_input = input("Approve web search? (yes/no): ").strip().lower()
            if approval_input not in ["yes", "y"]:
                print("Web search denied for this turn. Proceeding without tools.\n")
                tool_choice_override = "none"

        # Create a response using the agent
        response_kwargs = {
            "conversation": conversation.id,
            "extra_body": {
                "agent_reference": {
                    "name": agent.name,
                    "id": agent_version_id,
                    "type": "agent_reference",
                }
            },
            "input": "",
        }
        if tool_choice_override:
            response_kwargs["tool_choice"] = tool_choice_override

        response = openai_client.responses.create(**response_kwargs)

        # Check if the response output contains an MCP approval request.
        # Handle both object-style and dict-style items to be resilient across SDK versions.
        approval_request = None
        output_items = response.output if hasattr(response, "output") and response.output else []
        output_types = []

        for item in output_items:
            item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            output_types.append(item_type)
            if item_type == "mcp_approval_request":
                approval_request = item
                break

        if not approval_request and output_types:
            print(f"[No approval request in this turn. Output item types: {', '.join([t for t in output_types if t])}]")

        # Handle approval request if present
        if approval_request:
            approval_name = approval_request.get("name") if isinstance(approval_request, dict) else approval_request.name
            server_label = approval_request.get("server_label") if isinstance(approval_request, dict) else approval_request.server_label
            approval_arguments = approval_request.get("arguments") if isinstance(approval_request, dict) else approval_request.arguments
            approval_id = approval_request.get("id") if isinstance(approval_request, dict) else approval_request.id

            print(f"[Approval required for: {approval_name}]\n")
            print(f"Server: {server_label}")

            # Parse and display the arguments (optional, for transparency)
            import json
            try:
                args = json.loads(approval_arguments)
                print(f"Arguments: {json.dumps(args, indent=2)}\n")
            except Exception:
                print(f"Arguments: {approval_arguments}\n")

            # Prompt user for approval
            approval_input = input("Approve this action? (yes/no): ").strip().lower()

            if approval_input in ["yes", "y"]:
                print("Approving action...\n")
                approval_response = {
                    "type": "mcp_approval_response",
                    "approval_request_id": approval_id,
                    "approve": True,
                }
            else:
                print("Action denied.\n")
                approval_response = {
                    "type": "mcp_approval_response",
                    "approval_request_id": approval_id,
                    "approve": False,
                }

            # Add the approval response to the conversation
            openai_client.conversations.items.create(
                conversation_id=conversation.id,
                items=[approval_response]
            )

            # Get the actual response after approval/denial
            response = openai_client.responses.create(
                conversation=conversation.id,
                extra_body={
                    "agent_reference": {
                        "name": agent.name,
                        "id": agent_version_id,
                        "type": "agent_reference",
                    }
                },
                input=""
            )

        # Extract the response text
        if response and response.output_text:
            response_text = response.output_text

            print(f"{response_text}\n")

            # Check for citations if available
            if hasattr(response, "citations") and response.citations:
                print("\nSources:")
                for citation in response.citations:
                    print(f"  - {citation.content if hasattr(citation, 'content') else 'Knowledge Base'}")

            # Store in conversation history (client-side)
            conversation_history.append({
                "role": "assistant",
                "content": response_text
            })

            return response_text

        print("No response received.\n")
        return None
    except Exception as e:
        print(f"\n\nError: {str(e)}\n")
        return None


def display_conversation_history():
    """
    Display the full conversation history.
    """
    print("\n" + "="*60)
    print("CONVERSATION HISTORY")
    print("="*60 + "\n")
    
    for turn in conversation_history:
        role = turn["role"].upper()
        content = turn["content"]
        print(f"{role}: {content}\n")
    
    print("="*60 + "\n")


def main():
    """
    Main interaction loop.
    """
    print("Contoso Product Expert Agent")
    print("Ask questions about our outdoor and camping products.")
    print("Type 'history' to see conversation history, or 'quit' to exit.\n")
    
    while True:
        try:
            user_input = input("You: ").strip()
            
            if not user_input:
                continue
                
            if user_input.lower() == 'quit':
                print("\nEnding conversation...")
                break
                
            if user_input.lower() == 'history':
                display_conversation_history()
                continue
            
            # Send message and get response
            send_message_to_agent(user_input)
            
        except KeyboardInterrupt:
            print("\n\nInterrupted by user.")
            break
        except Exception as e:
            print(f"\nUnexpected error: {str(e)}\n")
    
    print("\nConversation ended.")


if __name__ == "__main__":
    main()
