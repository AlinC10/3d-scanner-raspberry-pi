import os
import traceback
from document_processing import process_document, process_directory
from ai_agent import ask_agent

def main():
    print("=" * 60)
    print("   AI Assistant for 3D Scanner (Terminal Interface)   ")
    print("=" * 60)
    
    while True:
        path = input("\nEnter path to file OR documentation folder (e.g. ../ai-docs): ").strip()
        path = path.strip('\"').strip('\'')
        
        if os.path.exists(path):
            break
        else:
            print(f"[ERROR] '{path}' not found. Please verify and try again.")

    doc_type_choice = input("Select document type:\n[1] User Manual\n[2] Technical Documentation\nChoice (1/2): ").strip()
    doc_type = "technical" if doc_type_choice == "2" else "user_manual"

    print(f"\n[INFO] Reading and processing data as '{doc_type}'... Please wait.")
    try:
        if os.path.isdir(path):
            chunks_count = process_directory(path, doc_type)
            print(f"[SUCCESS] Entire folder loaded! ({chunks_count} valid chunks).")
        else:
            chunks_count = process_document(path, doc_type)
            print(f"[SUCCESS] File loaded in {chunks_count} chunks.")
    except Exception as e:
        print("\n[FATAL ERROR] Could not process data:")
        traceback.print_exc()
        return

    print("\n" + "=" * 60)
    print("You can ask questions now. (Type 'exit' or 'q' to quit)")
    print("=" * 60)
    
    history = []
    
    while True:
        try:
            user_question = input("\nYou: ").strip()
            
            if user_question.lower() in ['exit', 'quit', 'q']:
                print("\nAssistant stopped. Have a productive day!")
                break
                
            if not user_question:
                continue
                
            print("AI is thinking...")
            
            result = ask_agent(user_question, history)
            response_text = result.get("response", "")
            
            print(f"\nAI: {response_text}")
                
            history.append({"role": "user", "text": user_question})
            history.append({"role": "assistant", "text": response_text})
            
            if len(history) > 10:
                history = history[-10:]
                
        except KeyboardInterrupt:
            print("\nAssistant forced stop. Goodbye!")
            break
        except Exception as e:
            print(f"\n[CHAT ERROR] An issue occurred: {str(e)}")

if __name__ == "__main__":
    main()