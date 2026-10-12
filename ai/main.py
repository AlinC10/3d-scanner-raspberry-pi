import os
import traceback
from document_processing import process_document, process_directory
from ai_agent import ask_agent

def main():
    print("=" * 60)
    print("   Asistent AI pentru Scanner 3D (Interfață Terminal)   ")
    print("=" * 60)
    
    while True:
        path = input("\nIntrodu calea către fișier SAU folderul cu documentație (ex: ../ai-docs): ").strip()
        path = path.strip('\"').strip('\'')
        
        if os.path.exists(path):
            break
        else:
            print(f"[EROARE] '{path}' nu a fost găsit. Te rog verifică și încearcă din nou.")

    print("\n[INFO] Citesc și procesez datele... Te rog așteaptă.")
    try:
        if os.path.isdir(path):
            chunks_count = process_directory(path)
            print(f"[SUCCES] Întregul folder a fost încărcat! ({chunks_count} secțiuni valide).")
        else:
            chunks_count = process_document(path)
            print(f"[SUCCES] Fișierul a fost încărcat în {chunks_count} secțiuni.")
    except Exception as e:
        print("\n[EROARE FATALĂ] Nu am putut procesa datele:")
        traceback.print_exc()
        return

    print("\n" + "=" * 60)
    print("Acum poți pune întrebări. (Scrie 'exit', 'quit' sau 'iesire' pentru a opri)")
    print("=" * 60)
    
    history = []
    
    while True:
        try:
            user_question = input("\nTu: ").strip()
            
            if user_question.lower() in ['exit', 'quit', 'iesire', 'q']:
                print("\nAsistent oprit. O zi productivă în continuare!")
                break
                
            if not user_question:
                continue
                
            print("AI se gândește...")
            
            result = ask_agent(user_question, history)
            response_text = result.get("response", "")
            
            print(f"\nAI: {response_text}")
                
            history.append({"role": "user", "text": user_question})
            history.append({"role": "assistant", "text": response_text})
            
            if len(history) > 10:
                history = history[-10:]
                
        except KeyboardInterrupt:
            print("\nAsistent oprit forțat. La revedere!")
            break
        except Exception as e:
            print(f"\n[EROARE CHAT] A apărut o problemă: {str(e)}")

if __name__ == "__main__":
    main()